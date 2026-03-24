import json
import os
import re
from datetime import date

import requests

from odoo import models
from odoo.exceptions import UserError


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'
    _GROQ_CHAT_COMPLETIONS_URL = 'https://api.groq.com/openai/v1/chat/completions'
    _GENERAL_SKILL_LEVELS = ('Beginner', 'Elementary', 'Intermediate', 'Advanced', 'Expert')
    _LANGUAGE_LEVELS = ('A1', 'A2', 'B1', 'B2', 'C1', 'C2')
    _LANGUAGE_SKILL_NAMES = {
        'arabic', 'french', 'english', 'spanish', 'german', 'italian', 'portuguese',
        'mandarin', 'chinese', 'japanese', 'korean', 'russian', 'dutch', 'turkish',
    }

    def _prepare_preview_payload(self, payload):
        if isinstance(payload, dict):
            preview = {}
            for key, value in payload.items():
                preview[key] = self._prepare_preview_payload(value)
            return preview

        if isinstance(payload, list):
            return [self._prepare_preview_payload(item) for item in payload]

        return payload

    def _open_extraction_preview(self, title, payload):
        self.ensure_one()
        wizard = self.env['applicant.extraction.preview.wizard'].create({
            'applicant_id': self.id,
            'preview_title': title,
            'extracted_data': json.dumps(
                self._prepare_preview_payload(payload),
                indent=4,
                ensure_ascii=False,
            ),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': title,
            'res_model': 'applicant.extraction.preview.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }

    def _is_pdf_attachment(self, attachment):
        mimetype = (attachment.mimetype or '').lower()
        filename = (attachment.name or '').lower()
        return mimetype == 'application/pdf' or filename.endswith('.pdf')

    def _select_cv_attachment(self):
        self.ensure_one()
        attachments = self.attachment_ids.sorted('id', reverse=True)

        pdf_attachments = attachments.filtered(lambda attachment: self._is_pdf_attachment(attachment))
        if not pdf_attachments:
            return self.env['ir.attachment'] # user error.

        # The main attachment is usually the uploaded CV in applicant flows.
        if self.message_main_attachment_id and self.message_main_attachment_id in pdf_attachments:
            return self.message_main_attachment_id

        keywords = ('cv', 'resume', 'curriculum', 'vitae')
        keyword_match = pdf_attachments.filtered(
            lambda attachment: any(keyword in (attachment.name or '').lower() for keyword in keywords)
        )
        return keyword_match[:1] or pdf_attachments[:1]

    def _get_cv_text(self, attachment):
        return (attachment.index_content or '').strip()

    def _is_likely_image_only_pdf(self, attachment):
        if not attachment or not self._is_pdf_attachment(attachment):
            return False
        return not bool(self._get_cv_text(attachment))

    def _verify_cv_is_text_based(self, attachment):
        cv_text = self._get_cv_text(attachment)
        if cv_text:
            return cv_text

        if self._is_likely_image_only_pdf(attachment):
            raise UserError(
                'The selected CV appears to be image-based (scanned PDF), so no readable text was found. '
                'Please upload a searchable PDF or run OCR first.'
            )

        raise UserError(
            'No extracted text found in the PDF attachment. '
            'Use a searchable PDF or enable attachment indexing in Odoo.'
        )

    def _get_groq_configuration(self):
        params = self.env['ir.config_parameter'].sudo()
        api_key = ''
        for key_name in ('scoring_candidates.groq_api_key', 'groq_api_key', 'GROQ_API_KEY'):
            key_value = (params.get_param(key_name) or '').strip()
            if key_value:
                api_key = key_value
                break

        if not api_key:
            for env_name in ('GROQ_API_KEY', 'GROQ_APIKEY'):
                env_value = (os.getenv(env_name) or '').strip()
                if env_value:
                    api_key = env_value
                    break

        model = (
            params.get_param('scoring_candidates.groq_model')
            or params.get_param('groq_model')
            or 'llama-3.3-70b-versatile'
        )
        if not api_key:
            raise UserError(
                'Missing Groq API key.\n'
                'Set one of these system parameters:\n'
                '- scoring_candidates.groq_api_key\n'
                '- groq_api_key\n'
                'Or set environment variable GROQ_API_KEY and restart Odoo.'
            )
        return api_key, model

    def _call_groq_json(self, system_prompt, user_prompt, max_tokens=1600):
        self.ensure_one()
        api_key, model = self._get_groq_configuration()
        request_payload = {
            'model': model,
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'response_format': {'type': 'json_object'},
            'max_tokens': max_tokens,
        }
        headers = {
            'Authorization': 'Bearer %s' % api_key,
            'Content-Type': 'application/json',
        }

        try:
            response = requests.post(
                self._GROQ_CHAT_COMPLETIONS_URL,
                headers=headers,
                json=request_payload,
                timeout=120,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise UserError('Groq request failed: %s' % error) from error

        response_payload = response.json()
        choices = response_payload.get('choices') or []
        if not choices:
            raise UserError('Groq response did not contain choices.')

        message = choices[0].get('message') or {}
        content = message.get('content')
        if not content:
            raise UserError('Groq response content is empty.')

        return self._extract_json_from_llm_output(content)

    def _extract_json_from_llm_output(self, raw_output):
        cleaned = (raw_output or '').strip()
        if cleaned.startswith('```'):
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            json_match = re.search(r'\{[\s\S]*\}', cleaned)
            if not json_match:
                raise UserError('Groq response does not contain a valid JSON object.')
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError as error:
                raise UserError('Unable to parse Groq JSON response: %s' % error) from error

    def _normalize_duration_text(self, duration_text):
        normalized = str(duration_text or '').strip()
        if not normalized:
            return ''

        normalized = normalized.replace('—', '-').replace('–', '-')
        normalized = re.sub(r'\s*-\s*', ' - ', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return normalized

    def _duration_to_year_interval(self, duration_text):
        normalized = self._normalize_duration_text(duration_text)
        if not normalized:
            return None

        year_values = [int(year) for year in re.findall(r'\b(?:19|20)\d{2}\b', normalized)]
        if not year_values:
            return None

        start_year = year_values[0]
        end_year = year_values[-1]

        if re.search(r'\b(present|current|now|ongoing)\b', normalized.lower()):
            end_year = date.today().year

        if end_year < start_year:
            start_year, end_year = end_year, start_year

        return (start_year, end_year)

    def _calculate_experience_years(self, experiences):
        intervals = []
        for experience in experiences:
            if not isinstance(experience, dict):
                continue
            interval = self._duration_to_year_interval(experience.get('duration'))
            if interval:
                intervals.append(interval)

        if not intervals:
            return 0.0

        intervals.sort(key=lambda interval: interval[0])
        merged_intervals = [list(intervals[0])]
        for start_year, end_year in intervals[1:]:
            last_start, last_end = merged_intervals[-1]
            if start_year <= last_end:
                merged_intervals[-1][1] = max(last_end, end_year)
            else:
                merged_intervals.append([start_year, end_year])

        total_years = 0.0
        for start_year, end_year in merged_intervals:
            span = end_year - start_year
            total_years += float(max(1, span))

        return round(total_years, 1)

    def _is_language_skill(self, skill_name):
        return str(skill_name or '').strip().lower() in self._LANGUAGE_SKILL_NAMES

    def _normalize_general_skill_level(self, raw_level):
        if raw_level is None:
            return ''

        level_text = str(raw_level).strip()
        if not level_text:
            return ''

        for label in self._GENERAL_SKILL_LEVELS:
            if level_text.lower() == label.lower():
                return label

        slash_match = re.match(r'^(\d+)\s*/\s*5$', level_text)
        if slash_match:
            numeric_level = int(slash_match.group(1))
            numeric_level = max(1, min(5, numeric_level))
            return self._GENERAL_SKILL_LEVELS[numeric_level - 1]

        try:
            numeric_level = int(float(level_text))
            numeric_level = max(1, min(5, numeric_level))
            return self._GENERAL_SKILL_LEVELS[numeric_level - 1]
        except (TypeError, ValueError):
            return ''

    def _normalize_language_skill_level(self, raw_level):
        if raw_level is None:
            return ''

        level_text = str(raw_level).strip().upper()
        if not level_text:
            return ''

        if level_text in self._LANGUAGE_LEVELS:
            return level_text

        slash_match = re.match(r'^(\d+)\s*/\s*6$', level_text)
        if slash_match:
            numeric_level = int(slash_match.group(1))
            numeric_level = max(1, min(6, numeric_level))
            return self._LANGUAGE_LEVELS[numeric_level - 1]

        try:
            numeric_level = int(float(level_text))
            numeric_level = max(1, min(6, numeric_level))
            return self._LANGUAGE_LEVELS[numeric_level - 1]
        except (TypeError, ValueError):
            return ''

    def _normalize_ai_profile(self, payload):
        if not isinstance(payload, dict):
            payload = {}

        education = payload.get('education') if isinstance(payload.get('education'), dict) else {}
        experiences = payload.get('experiences') if isinstance(payload.get('experiences'), list) else []
        skills = payload.get('skills') if isinstance(payload.get('skills'), dict) else {}

        normalized_experiences = []
        for experience in experiences:
            if not isinstance(experience, dict):
                continue
            tasks = experience.get('tasks') if isinstance(experience.get('tasks'), list) else []
            skills_pertinents = (
                experience.get('skills_pertinents')
                if isinstance(experience.get('skills_pertinents'), list)
                else []
            )
            normalized_experiences.append({
                'title': experience.get('title') or '',
                'company': experience.get('company') or '',
                'duration': self._normalize_duration_text(experience.get('duration')),
                'tasks': [str(task).strip() for task in tasks if str(task).strip()],
                'skills_pertinents': [
                    str(skill_name).strip()
                    for skill_name in skills_pertinents
                    if str(skill_name).strip()
                ],
            })

        normalized_skills = {}
        for skill_name, skill_level in skills.items():
            if not skill_name:
                continue
            normalized_name = str(skill_name).strip()
            if not normalized_name:
                continue

            if self._is_language_skill(normalized_name):
                normalized_level = self._normalize_language_skill_level(skill_level)
            else:
                normalized_level = self._normalize_general_skill_level(skill_level)

            if normalized_level:
                normalized_skills[normalized_name] = normalized_level

        certifications = payload.get('certification')
        if not isinstance(certifications, list):
            certifications = []
        normalized_certifications = [
            str(certification).strip()
            for certification in certifications
            if str(certification).strip()
        ]

        experience_years = self._calculate_experience_years(normalized_experiences)

        return {
            'id': self.id,
            'name': payload.get('name') or self.partner_name or '',
            'education': {
                'degree': education.get('degree') or '',
                'field': education.get('field') or '',
                'university': education.get('university') or '',
            },
            'experiences': normalized_experiences,
            'experience_years': experience_years,
            'certification': normalized_certifications,
            'skills': normalized_skills,
        }

    def _extract_profile_with_groq(self, cv_text):
        self.ensure_one()
        system_prompt = (
            'You are an expert CV parser. '\
            'Return ONLY valid JSON with this exact top-level structure: '\
            '{"id": int, "name": str, "education": {"degree": str, "field": str, "university": str}, '\
            '"experiences": [{"title": str, "company": str, "duration": str, "tasks": [str], "skills_pertinents": [str]}], '\
            '"experience_years": float, '\
            '"certification": [str], '\
            '"skills": {"skill_name": str_level}}. '\
            'For technical and soft skills, use exactly one of: Beginner, Elementary, Intermediate, Advanced, Expert. '\
            'For language skills, use exactly one of: A1, A2, B1, B2, C1, C2. '\
            'Do not include markdown, comments, or explanations.'
        )
        user_prompt = (
            'Extract CV data from the text below. '\
            'Set "id" to %s. '\
            'If a value is missing, use empty string, empty list, or empty object. '\
            'Always include skills_pertinents for each experience when possible. '\
            'Use string proficiency levels (not numeric).\n\nCV TEXT:\n%s'
        ) % (self.id, cv_text[:16000])

        ai_payload = self._call_groq_json(system_prompt, user_prompt)
        return self._normalize_ai_profile(ai_payload)

    def _skill_level_to_score5(self, raw_level):
        if raw_level is None:
            return 0

        level_text = str(raw_level).strip()
        if not level_text:
            return 0

        general_map = {
            'beginner': 1,
            'elementary': 2,
            'intermediate': 3,
            'advanced': 4,
            'expert': 5,
        }
        language_map = {
            'A1': 1,
            'A2': 2,
            'B1': 3,
            'B2': 4,
            'C1': 5,
            'C2': 5,
        }

        lowered = level_text.lower()
        uppered = level_text.upper()
        if lowered in general_map:
            return general_map[lowered]
        if uppered in language_map:
            return language_map[uppered]

        try:
            numeric = int(float(level_text))
            return max(0, min(5, numeric))
        except (TypeError, ValueError):
            return 0

    def _build_scoring_inputs(self, applicant_data, job_data):
        applicant_skills = applicant_data.get('skills') if isinstance(applicant_data.get('skills'), dict) else {}
        job_skills = job_data.get('skills') if isinstance(job_data.get('skills'), dict) else {}

        candidate_payload = {
            'id': applicant_data.get('id') or self.id,
            'name': applicant_data.get('name') or self.partner_name or '',
            'education': applicant_data.get('education') or {},
            'experiences': applicant_data.get('experiences') or [],
            'skills': {
                str(skill_name).strip(): self._skill_level_to_score5(skill_level)
                for skill_name, skill_level in applicant_skills.items()
                if str(skill_name).strip()
            },
            'experience_years': applicant_data.get('experience_years') or 0.0,
            'certification': applicant_data.get('certification') or [],
        }

        required_skills = [str(skill_name).strip() for skill_name in job_skills.keys() if str(skill_name).strip()]
        required_skill_levels = {
            str(skill_name).strip(): self._skill_level_to_score5(skill_level)
            for skill_name, skill_level in job_skills.items()
            if str(skill_name).strip()
        }
        job_payload = {
            'id': job_data.get('job_id'),
            'title': job_data.get('title') or '',
            'education': job_data.get('education') or '',
            'required_skills': required_skills,
            'required_skill_levels': required_skill_levels,
            'nice_to_have': job_data.get('nice_to_have') or [],
            'min_experience_years': job_data.get('min_exp_years') or 0.0,
        }
        return candidate_payload, job_payload

    def _normalize_skill_key(self, value):
        normalized = str(value or '').strip().lower()
        if not normalized:
            return ''
        normalized = re.sub(r'[^a-z0-9\s\+#\./-]', ' ', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return normalized

    def _round_half_up(self, value):
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return 0
        if numeric_value <= 0:
            return 0
        return int(numeric_value + 0.5)

    def _build_candidate_skill_index(self, candidate_skills):
        index = {}
        if not isinstance(candidate_skills, dict):
            return index

        for raw_name, raw_level in candidate_skills.items():
            normalized_name = self._normalize_skill_key(raw_name)
            if not normalized_name:
                continue
            level = 0
            try:
                level = int(float(raw_level or 0))
            except (TypeError, ValueError):
                level = 0
            index[normalized_name] = max(0, min(5, level))

        return index

    def _extract_required_language_skills(self, required_skills):
        language_skills = []
        for skill_name in required_skills:
            normalized_name = self._normalize_skill_key(skill_name)
            if not normalized_name:
                continue
            if normalized_name in self._LANGUAGE_SKILL_NAMES:
                language_skills.append(skill_name)
        return language_skills

    def _extract_required_technical_skills(self, required_skills):
        language_keys = {self._normalize_skill_key(name) for name in self._LANGUAGE_SKILL_NAMES}
        tech_skills = []
        for skill_name in required_skills:
            normalized_name = self._normalize_skill_key(skill_name)
            if not normalized_name:
                continue
            if normalized_name in language_keys:
                continue
            tech_skills.append(skill_name)
        return tech_skills

    def _candidate_evidence_text(self, candidate_payload):
        chunks = []
        for skill_name in (candidate_payload.get('skills') or {}).keys():
            chunks.append(str(skill_name or ''))

        for experience in (candidate_payload.get('experiences') or []):
            if not isinstance(experience, dict):
                continue
            chunks.append(str(experience.get('title') or ''))
            chunks.append(str(experience.get('company') or ''))
            for task in experience.get('tasks') or []:
                chunks.append(str(task or ''))
            for skill_name in experience.get('skills_pertinents') or []:
                chunks.append(str(skill_name or ''))

        return ' '.join(chunks).lower()

    def _extract_degree_rank(self, degree_text):
        normalized = self._normalize_skill_key(degree_text)
        if not normalized:
            return 0

        bac_match = re.search(r'bac\s*\+\s*(\d+)', normalized)
        if bac_match:
            return int(bac_match.group(1))

        if 'phd' in normalized or 'doctorat' in normalized or 'doctorate' in normalized:
            return 8
        if 'master' in normalized or 'engineer' in normalized or 'ing' in normalized:
            return 5
        if 'bachelor' in normalized or 'licence' in normalized or 'license' in normalized:
            return 3
        if 'bts' in normalized or 'dut' in normalized or 'associate' in normalized:
            return 2
        if 'high school' in normalized or 'bac' in normalized:
            return 0

        return 0

    def _score_education_component(self, candidate_payload, job_payload):
        candidate_education = candidate_payload.get('education') or {}
        candidate_degree = str(candidate_education.get('degree') or '')
        candidate_rank = self._extract_degree_rank(candidate_degree)

        required_degree_text = str(job_payload.get('education') or '')
        required_rank = self._extract_degree_rank(required_degree_text)

        if required_rank > 0 and candidate_rank < required_rank:
            return 0, (
                'Candidate degree below requirement '
                '(candidate_rank=%s < required_rank=%s).' % (candidate_rank, required_rank)
            ), ['Education level below requirement']

        if required_rank > 0 and candidate_rank >= required_rank:
            final_score = 15
        else:
            final_score = 0

        explanation = (
            'candidate_degree="%s", required_degree="%s", '
            'candidate_rank=%s, required_rank=%s, education_score=%s'
        ) % (candidate_degree, required_degree_text, candidate_rank, required_rank, final_score)
        return final_score, explanation, []

    def _normalize_match_score_payload(self, payload):
        if not isinstance(payload, dict):
            payload = {}

        score_details = payload.get('score_details') if isinstance(payload.get('score_details'), dict) else {}
        caps = {
            'competences_techniques': 40,
            'experience': 35,
            'education': 15,
            'langues': 10,
        }

        normalized_details = {}
        for key, max_value in caps.items():
            raw_value = score_details.get(key, 0)
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                value = 0
            normalized_details[key] = max(0, min(max_value, value))

        explanation = payload.get('explanation') if isinstance(payload.get('explanation'), dict) else {}
        normalized_explanation = {
            'competences_techniques': str(explanation.get('competences_techniques') or '').strip(),
            'experience': str(explanation.get('experience') or '').strip(),
            'education': str(explanation.get('education') or '').strip(),
            'langues': str(explanation.get('langues') or '').strip(),
        }

        feedback = payload.get('ai_feedback') if isinstance(payload.get('ai_feedback'), dict) else {}
        fit_level = str(feedback.get('fit_level') or '').strip()
        if fit_level not in ('Strong Fit', 'Moderate Fit', 'Weak Fit'):
            fit_level = 'Moderate Fit'

        normalized_feedback = {
            'fit_level': fit_level,
            'summary': str(feedback.get('summary') or '').strip(),
            'strengths': [str(item).strip() for item in (feedback.get('strengths') or []) if str(item).strip()],
            'risks': [str(item).strip() for item in (feedback.get('risks') or []) if str(item).strip()],
            'ambiguities_to_verify': [
                str(item).strip() for item in (feedback.get('ambiguities_to_verify') or []) if str(item).strip()
            ],
            'interview_questions': [
                str(item).strip() for item in (feedback.get('interview_questions') or []) if str(item).strip()
            ],
            'recommendation': str(feedback.get('recommendation') or '').strip(),
        }

        return {
            'explanation': normalized_explanation,
            'score_details': normalized_details,
            'score_total': sum(normalized_details.values()),
            'matched_skills': [str(item).strip() for item in (payload.get('matched_skills') or []) if str(item).strip()],
            'missing_requirements': [
                str(item).strip()
                for item in (payload.get('missing_requirements') or [])
                if str(item).strip()
            ],
            'bonus_matches': [str(item).strip() for item in (payload.get('bonus_matches') or []) if str(item).strip()],
            'ai_feedback': normalized_feedback,
            'status': 'done',
        }

    def _build_fallback_feedback(self, score_details, matched_skills, missing_requirements):
        total_score = int(sum((score_details or {}).values()))
        if total_score >= 75:
            fit_level = 'Strong Fit'
            recommendation = 'Proceed'
        elif total_score >= 55:
            fit_level = 'Moderate Fit'
            recommendation = 'Proceed with caution'
        else:
            fit_level = 'Weak Fit'
            recommendation = 'Reject'

        top_strengths = list(matched_skills or [])[:8]
        top_risks = list(missing_requirements or [])[:8]
        summary_lines = [
            'Deterministic scoring summary for recruiter review.',
            'Total score=%s/100.' % total_score,
            'Technical=%s/40, Experience=%s/35, Education=%s/15, Languages=%s/10.' % (
                score_details.get('competences_techniques', 0),
                score_details.get('experience', 0),
                score_details.get('education', 0),
                score_details.get('langues', 0),
            ),
            'This fallback feedback is generated without LLM narrative and should be treated as a concise risk checklist.',
            'Prioritize manual validation of level claims, recency of experience, and education equivalence before final decision.',
        ]
        return {
            'fit_level': fit_level,
            'summary': ' '.join(summary_lines),
            'strengths': top_strengths,
            'risks': top_risks,
            'ambiguities_to_verify': [
                'Are claimed proficiency levels backed by concrete project evidence?',
                'Do date ranges in experiences contain overlaps or gaps affecting total years?',
                'Is degree level equivalent to the required local market standard?',
            ],
            'interview_questions': [
                'What type of work environment helps you perform at your best, and why?',
                'Can you describe a conflict with a colleague and how you resolved it?',
                'Which responsibilities in your last role did you own end-to-end?',
            ],
            'recommendation': recommendation,
        }

    def _generate_ai_recruiter_feedback(self, candidate_payload, job_payload, scoring_payload):
        self.ensure_one()
        system_prompt = (
            'You are a senior recruiter assistant. '\
            'Use only provided evidence. '\
            'Do not recalculate scores. '\
            'Highlight ambiguities explicitly. '\
            'Be detailed and concrete for recruiter actionability. '\
            'Write a long summary (at least 120 words) with balanced strengths and risks. '\
            'Provide 5-8 strengths, 5-8 risks, 4-8 ambiguities_to_verify, and 6-10 interview_questions. '\
            'interview_questions must be RH-only (behavior, motivation, communication, culture fit). '\
            'Do not include technical, coding, architecture, or tool-specific interview questions. '\
            'Return ONLY valid JSON with this exact structure: '\
            '{"fit_level": "Strong Fit|Moderate Fit|Weak Fit", "summary": str, '\
            '"strengths": [str], "risks": [str], "ambiguities_to_verify": [str], '\
            '"interview_questions": [str], "recommendation": "Proceed|Proceed with caution|Reject"}.'
        )
        prompt_payload = {
            'candidate': {
                'name': candidate_payload.get('name'),
                'education': candidate_payload.get('education') or {},
                'experience_years': candidate_payload.get('experience_years') or 0.0,
                'skills': candidate_payload.get('skills') or {},
            },
            'job': {
                'title': job_payload.get('title') or '',
                'education': job_payload.get('education') or '',
                'required_skills': job_payload.get('required_skills') or [],
                'required_skill_levels': job_payload.get('required_skill_levels') or {},
                'min_experience_years': job_payload.get('min_experience_years') or 0.0,
            },
            'scoring': {
                'score_total': scoring_payload.get('score_total', 0),
                'score_details': scoring_payload.get('score_details') or {},
                'matched_skills': scoring_payload.get('matched_skills') or [],
                'missing_requirements': scoring_payload.get('missing_requirements') or [],
                'explanation': scoring_payload.get('explanation') or {},
            },
        }
        user_prompt = (
            'Analyze candidate fit for recruiter decision support. '\
            'Output detailed, evidence-based feedback and avoid generic wording. '\
            'Interview questions must be non-technical and suitable for RH screening only. '\
            'When evidence is missing, state uncertainty clearly in ambiguities_to_verify.\n%s'
        ) % json.dumps(
            prompt_payload,
            ensure_ascii=False,
        )
        try:
            ai_feedback = self._call_groq_json(system_prompt, user_prompt, max_tokens=2400)
            normalized = self._normalize_match_score_payload({'ai_feedback': ai_feedback}).get('ai_feedback')
            if normalized:
                return normalized
        except Exception:
            pass

        return self._build_fallback_feedback(
            scoring_payload.get('score_details') or {},
            scoring_payload.get('matched_skills') or [],
            scoring_payload.get('missing_requirements') or [],
        )

    def _score_applicant_against_job_with_groq(self, applicant_data, job_data):
        self.ensure_one()
        candidate_payload, job_payload = self._build_scoring_inputs(applicant_data, job_data)
        candidate_skill_index = self._build_candidate_skill_index(candidate_payload.get('skills') or {})

        required_skills = job_payload.get('required_skills') or []
        required_skill_levels = job_payload.get('required_skill_levels') or {}
        tech_skills = self._extract_required_technical_skills(required_skills)
        language_skills = self._extract_required_language_skills(required_skills)

        matched_skills = []
        missing_requirements = []
        explanation = {}

        # Component 1: Technical skills (0-40).
        tech_points_raw = 0.0
        if tech_skills:
            points_per_skill = 40.0 / len(tech_skills)
            tech_calc_parts = ['N=%s, pts_per_skill=%.2f' % (len(tech_skills), points_per_skill)]
            for skill_name in tech_skills:
                normalized_name = self._normalize_skill_key(skill_name)
                level = candidate_skill_index.get(normalized_name, 0)
                required_level = self._skill_level_to_score5(required_skill_levels.get(skill_name) or 0)
                effective_required = max(1, required_level)
                ratio = min(float(level) / float(effective_required), 1.0)
                contribution = points_per_skill * ratio
                tech_points_raw += contribution
                tech_calc_parts.append(
                    '%s candidate=%s required=%s -> %.2f*min(%s/%s,1)=%.2f'
                    % (skill_name, level, effective_required, points_per_skill, level, effective_required, contribution)
                )
                if level >= effective_required:
                    matched_skills.append(skill_name)
                else:
                    missing_requirements.append('Missing required skill: %s' % skill_name)
                if level > 0 and level < effective_required:
                    missing_requirements.append(
                        'Required level not met: %s (%s/%s)' % (skill_name, level, effective_required)
                    )
            explanation['competences_techniques'] = ' | '.join(tech_calc_parts)
        else:
            explanation['competences_techniques'] = 'No required technical skills found in job payload.'

        competences_techniques = max(0, min(40, self._round_half_up(tech_points_raw)))

        # Component 2: Experience (0-35).
        candidate_years = float(candidate_payload.get('experience_years') or 0.0)
        required_years = float(job_payload.get('min_experience_years') or 0.0)
        if candidate_years >= required_years:
            experience_raw = 15.0 + min((candidate_years - required_years) * 5.0, 25.0)
            experience_score = max(0, min(35, self._round_half_up(experience_raw)))
            explanation['experience'] = (
                'candidate_years=%.1f >= required_years=%.1f -> '
                '15 + min((%.1f-%.1f)*5,25) = %.2f -> %s'
            ) % (candidate_years, required_years, candidate_years, required_years, experience_raw, experience_score)
        else:
            experience_score = 0
            explanation['experience'] = (
                'candidate_years=%.1f < required_years=%.1f -> score=0'
            ) % (candidate_years, required_years)
            missing_requirements.append('Insufficient experience years')

        # Component 3: Education (0-15).
        education_score, education_explanation, education_missing = self._score_education_component(
            candidate_payload,
            job_payload,
        )
        explanation['education'] = education_explanation
        missing_requirements.extend(education_missing)

        # Component 4: Languages (0-10).
        lang_points_raw = 0.0
        if language_skills:
            points_per_language = 10.0 / len(language_skills)
            language_parts = ['L=%s, pts_per_lang=%.2f' % (len(language_skills), points_per_language)]
            for language_name in language_skills:
                normalized_name = self._normalize_skill_key(language_name)
                level = candidate_skill_index.get(normalized_name, 0)
                required_level = self._skill_level_to_score5(required_skill_levels.get(language_name) or 0)
                effective_required = max(1, required_level)
                ratio = min(float(level) / float(effective_required), 1.0)
                contribution = points_per_language * ratio
                lang_points_raw += contribution
                language_parts.append(
                    '%s candidate=%s required=%s -> %.2f*min(%s/%s,1)=%.2f'
                    % (language_name, level, effective_required, points_per_language, level, effective_required, contribution)
                )
                if level >= effective_required:
                    matched_skills.append(language_name)
                else:
                    missing_requirements.append('Missing required language: %s' % language_name)
                if level > 0 and level < effective_required:
                    missing_requirements.append(
                        'Required level not met: %s (%s/%s)' % (language_name, level, effective_required)
                    )
            explanation['langues'] = ' | '.join(language_parts)
            langues_score = max(0, min(10, self._round_half_up(lang_points_raw)))
        else:
            langues_score = 10
            explanation['langues'] = 'No required language in job.required_skills -> score=10.'

        score_details = {
            'competences_techniques': competences_techniques,
            'experience': experience_score,
            'education': education_score,
            'langues': langues_score,
        }

        payload = {
            'explanation': explanation,
            'score_details': score_details,
            'score_total': sum(score_details.values()),
            'matched_skills': sorted(set(matched_skills), key=lambda item: item.lower()),
            'missing_requirements': sorted(set(missing_requirements), key=lambda item: item.lower()),
            'bonus_matches': [],
            'status': 'done',
        }
        normalized_payload = self._normalize_match_score_payload(payload)
        normalized_payload['ai_feedback'] = self._generate_ai_recruiter_feedback(
            candidate_payload,
            job_payload,
            normalized_payload,
        )
        return self._normalize_match_score_payload(normalized_payload)

    def get_applicant_job_match_data(self):
        self.ensure_one()
        applicant_data = self.get_extracted_applicant_data()
        job_list = self.get_extracted_job_data()
        if not job_list:
            raise UserError('No linked job data found for this applicant.')

        job_data = job_list[0]
        match_data = self._score_applicant_against_job_with_groq(applicant_data, job_data)
        return {
            'applicant': applicant_data,
            'job': job_data,
            'matching': match_data,
        }

    def get_extracted_job_data(self):
        self.ensure_one()
        return self.env['extract.job.info'].get_job_data(applicant=self)

    def get_extracted_cv_data(self):
        self.ensure_one()
        attachment = self._select_cv_attachment()
        if not attachment:
            return {
                'attachment_id': None,
                'filename': None,
                'mimetype': None,
            }

        return {
            'attachment_id': attachment.id,
            'filename': attachment.name,
            'mimetype': attachment.mimetype,
        }

    def get_extracted_applicant_data(self):
        self.ensure_one()
        attachment = self._select_cv_attachment()
        if not attachment:
            raise UserError('No PDF CV found for this applicant.')

        cv_text = self._verify_cv_is_text_based(attachment)

        return self._extract_profile_with_groq(cv_text)

    def action_show_job_extraction_preview(self):
        self.ensure_one()
        return self._open_extraction_preview('Job Extraction Preview', self.get_extracted_job_data())

    def action_show_applicant_extraction_preview(self):
        self.ensure_one()
        return self._open_extraction_preview('Applicant Extraction Preview', self.get_extracted_applicant_data())

    def action_show_applicant_job_match_preview(self):
        self.ensure_one()
        return self._open_extraction_preview('Applicant vs Job Match Preview', self.get_applicant_job_match_data())
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
        job_payload = {
            'id': job_data.get('job_id'),
            'title': job_data.get('title') or '',
            'education': job_data.get('education') or '',
            'required_skills': required_skills,
            'nice_to_have': job_data.get('nice_to_have') or [],
            'min_experience_years': job_data.get('min_exp_years') or 0.0,
        }
        return candidate_payload, job_payload

    def _normalize_match_score_payload(self, payload):
        if not isinstance(payload, dict):
            payload = {}

        score_details = payload.get('score_details') if isinstance(payload.get('score_details'), dict) else {}
        caps = {
            'competences_techniques': 40,
            'experience': 25,
            'education': 15,
            'langues': 10,
            'bonus': 10,
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
            'bonus': str(explanation.get('bonus') or '').strip(),
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
            'status': 'done',
        }

    def _score_applicant_against_job_with_groq(self, applicant_data, job_data):
        self.ensure_one()
        candidate_payload, job_payload = self._build_scoring_inputs(applicant_data, job_data)

        system_prompt = """You are an expert HR scoring assistant. Evaluate a candidate's CV against a job position and return a structured JSON score.

CRITICAL RULE: The integer you write in score_details for each component MUST be the EXACT result of the formula computation - nothing more, nothing less. Never round up.

You must score the candidate across exactly 5 components. Total max score is 100 points.

COMPONENT 1: Competences techniques (0-40 pts)
- Use only job.required_skills excluding known languages.
- For each required tech skill, if exact key exists in candidate.skills (0..5): score_i = (40/N) * (level/5), else 0.
- competences_techniques = round(sum(score_i)), capped at 40.

COMPONENT 2: Experience (0-25 pts)
- candidate_years = numeric years from candidate experience durations.
- required_years = job.min_experience_years.
- If candidate_years >= required_years: experience = 15 + min((candidate_years-required_years)*5, 10), else 0.

COMPONENT 3: Education (0-15 pts)
- Use degree level and field relevance.

COMPONENT 4: Langues (0-10 pts)
- Use only required language skills from required_skills.
- If no required languages -> 10.
- Else for each language: contribution = (10/L) * (level/5) when present, else 0.
- langues = round(sum(contributions)), capped at 10.

COMPONENT 5: Bonus / nice-to-have (0-10 pts)
- Use job.nice_to_have.
- If empty -> 0.
- Else each matched nice-to-have in candidate experiences/tasks or candidate.skills gives (10/B).
- bonus = round(sum(contributions)), capped at 10.

FINAL RULES:
- score_total = competences_techniques + experience + education + langues + bonus.
- Return ONLY valid JSON.

OUTPUT FORMAT:
{
  "explanation": {
    "competences_techniques": "string",
    "experience": "string",
    "education": "string",
    "langues": "string",
    "bonus": "string"
  },
  "score_details": {
    "competences_techniques": 0,
    "experience": 0,
    "education": 0,
    "langues": 0,
    "bonus": 0
  },
  "score_total": 0,
  "matched_skills": ["string"],
  "missing_requirements": ["string"],
  "bonus_matches": ["string"],
  "status": "done"
}"""

        user_prompt = (
            'Evaluate the following candidate against the job position and return only the JSON score object.\n\n'
            'JOB:\n%s\n\n'
            'CANDIDATE:\n%s'
        ) % (
            json.dumps(job_payload, indent=2, ensure_ascii=False),
            json.dumps(candidate_payload, indent=2, ensure_ascii=False),
        )

        ai_payload = self._call_groq_json(system_prompt, user_prompt, max_tokens=2200)
        return self._normalize_match_score_payload(ai_payload)

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
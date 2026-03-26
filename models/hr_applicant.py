import json
import logging
import os
import re
from html import escape
from datetime import date

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'
    _GROQ_CHAT_COMPLETIONS_URL = 'https://api.groq.com/openai/v1/chat/completions'
    _CV_EXTRACTION_CHUNK_SIZE = 16000
    _CV_EXTRACTION_CHUNK_OVERLAP = 1000
    _CV_EXTRACTION_MAX_CHUNKS = 3
    _GENERAL_SKILL_LEVELS = ('Beginner', 'Elementary', 'Intermediate', 'Advanced', 'Expert')
    _GENERAL_LEVEL_BY_SCORE5 = {
        1: 'Beginner',
        2: 'Elementary',
        3: 'Intermediate',
        4: 'Advanced',
        5: 'Expert',
    }
    _LANGUAGE_LEVELS = ('A1', 'A2', 'B1', 'B2', 'C1', 'C2')
    _LANGUAGE_SKILL_NAMES = {
        'arabic', 'french', 'english', 'spanish', 'german', 'italian', 'portuguese',
        'mandarin', 'chinese', 'japanese', 'korean', 'russian', 'dutch', 'turkish',
    }
    _LANGUAGE_SKILL_ALIASES = {
        'anglais': 'english',
        'francais': 'french',
        'espagnol': 'spanish',
        'allemand': 'german',
        'italien': 'italian',
        'portugais': 'portuguese',
        'arabe': 'arabic',
        'chinois': 'chinese',
        'mandarin chinese': 'mandarin',
        'japonais': 'japanese',
        'coreen': 'korean',
        'russe': 'russian',
        'neerlandais': 'dutch',
        'turc': 'turkish',
        'langue anglaise': 'english',
        'langue francaise': 'french',
        'french language': 'french',
        'english language': 'english',
    }
    _SKILL_SYNONYMS = {
        'js': 'javascript',
        'node': 'node.js',
        'nodejs': 'node.js',
        'reactjs': 'react',
        'react.js': 'react',
        'vuejs': 'vue',
        'vue.js': 'vue',
        'ts': 'typescript',
        'py': 'python',
        'postgres': 'postgresql',
        'mongo': 'mongodb',
        'c sharp': 'c#',
        'csharp': 'c#',
        'dotnet': '.net',
    }
    _MONTH_NAME_TO_NUMBER = {
        'jan': 1, 'january': 1,
        'feb': 2, 'february': 2,
        'mar': 3, 'march': 3,
        'apr': 4, 'april': 4,
        'may': 5,
        'jun': 6, 'june': 6,
        'jul': 7, 'july': 7,
        'aug': 8, 'august': 8,
        'sep': 9, 'sept': 9, 'september': 9,
        'oct': 10, 'october': 10,
        'nov': 11, 'november': 11,
        'dec': 12, 'december': 12,
    }

    score_total = fields.Integer(string='Score Total', readonly=True, copy=False)
    ai_feedback = fields.Html(string='AI Feedback', readonly=True, copy=False)
    applicant_extracted_json = fields.Text(string='Applicant Extracted JSON', readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        applicants = super().create(vals_list)
        applicants._auto_run_scoring_if_ready()
        return applicants

    def write(self, vals):
        result = super().write(vals)
        if 'job_id' in vals:
            self._auto_run_scoring_if_ready()
        return result

    def _auto_run_scoring_if_ready(self):
        for applicant in self:
            if not applicant.job_id:
                continue

            attachment = applicant._select_cv_attachment()
            if not attachment:
                continue

            try:
                applicant.get_applicant_job_match_data()
            except UserError as error:
                _logger.info(
                    'Auto scoring skipped for applicant %s: %s',
                    applicant.id,
                    error,
                )
            except Exception as error:
                _logger.warning(
                    'Auto scoring failed for applicant %s: %s',
                    applicant.id,
                    error,
                    exc_info=True,
                )

    def _ai_feedback_to_html(self, feedback):
        if not isinstance(feedback, dict):
            return ''

        def _list_html(values):
            items = [str(item).strip() for item in (values or []) if str(item).strip()]
            if not items:
                return '<p><i>Aucun</i></p>'
            return '<ul>%s</ul>' % ''.join('<li>%s</li>' % escape(item) for item in items)

        fit_level = escape(str(feedback.get('fit_level') or ''))
        recommendation = escape(str(feedback.get('recommendation') or ''))
        summary = escape(str(feedback.get('summary') or ''))

        sections = [
            '<p><b>Niveau d adequation :</b> %s</p>' % fit_level,
            '<p><b>Recommandation :</b> %s</p>' % recommendation,
            '<p><b>Resume</b><br/>%s</p>' % summary,
            '<p><b>Points forts</b></p>%s' % _list_html(feedback.get('strengths')),
            '<p><b>Risques</b></p>%s' % _list_html(feedback.get('risks')),
            '<p><b>Ambiguites a verifier</b></p>%s' % _list_html(feedback.get('ambiguities_to_verify')),
            '<p><b>Questions RH</b></p>%s' % _list_html(feedback.get('interview_questions')),
        ]
        return ''.join(sections)

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

    def _split_cv_text_chunks(self, cv_text):
        normalized = str(cv_text or '').strip()
        if not normalized:
            return []

        chunk_size = int(self._CV_EXTRACTION_CHUNK_SIZE)
        overlap = int(self._CV_EXTRACTION_CHUNK_OVERLAP)
        max_chunks = int(self._CV_EXTRACTION_MAX_CHUNKS)
        if chunk_size <= 0:
            return [normalized]

        overlap = max(0, min(overlap, max(0, chunk_size - 1)))
        step = max(1, chunk_size - overlap)

        chunks = []
        start = 0
        while start < len(normalized) and len(chunks) < max_chunks:
            end = min(start + chunk_size, len(normalized))
            chunks.append(normalized[start:end])
            if end >= len(normalized):
                break
            start += step
        return chunks

    def _merge_profiles(self, profiles):
        cleaned_profiles = [profile for profile in (profiles or []) if isinstance(profile, dict)]
        if not cleaned_profiles:
            return {
                'id': self.id,
                'name': self.partner_name or '',
                'education': {'degree': '', 'field': '', 'university': ''},
                'experiences': [],
                'experience_years': 0.0,
                'certification': [],
                'skills': {},
                'extraction_warnings': ['No profile data was extracted from the CV text.'],
            }

        merged = {
            'id': self.id,
            'name': '',
            'education': {'degree': '', 'field': '', 'university': ''},
            'experiences': [],
            'experience_years': 0.0,
            'certification': [],
            'skills': {},
        }

        experience_seen = set()
        certification_seen = set()
        skill_scores = {}

        for profile in cleaned_profiles:
            if not merged['name']:
                merged['name'] = str(profile.get('name') or '').strip()

            education = profile.get('education') if isinstance(profile.get('education'), dict) else {}
            for field_name in ('degree', 'field', 'university'):
                if not merged['education'][field_name]:
                    merged['education'][field_name] = str(education.get(field_name) or '').strip()

            for experience in (profile.get('experiences') or []):
                if not isinstance(experience, dict):
                    continue
                fingerprint = (
                    str(experience.get('title') or '').strip().lower(),
                    str(experience.get('company') or '').strip().lower(),
                    str(experience.get('duration') or '').strip().lower(),
                )
                if fingerprint in experience_seen:
                    continue
                experience_seen.add(fingerprint)
                merged['experiences'].append(experience)

            for certification in (profile.get('certification') or []):
                normalized_certification = str(certification or '').strip()
                if not normalized_certification:
                    continue
                fingerprint = normalized_certification.lower()
                if fingerprint in certification_seen:
                    continue
                certification_seen.add(fingerprint)
                merged['certification'].append(normalized_certification)

            for skill_name, raw_level in (profile.get('skills') or {}).items():
                canonical_skill = self._canonical_skill_name(skill_name)
                if not canonical_skill:
                    continue
                score = self._skill_level_to_score5(raw_level)
                if score <= 0:
                    continue
                skill_scores[canonical_skill] = max(skill_scores.get(canonical_skill, 0), score)

        merged['skills'] = {
            skill_name: self._GENERAL_LEVEL_BY_SCORE5.get(level, 'Beginner')
            for skill_name, level in skill_scores.items()
        }
        merged['experience_years'] = self._calculate_experience_years(merged['experiences'])
        merged['extraction_warnings'] = self._build_extraction_warnings(merged)
        return merged

    def _build_extraction_warnings(self, applicant_profile):
        warnings = []
        profile = applicant_profile if isinstance(applicant_profile, dict) else {}

        if not str(profile.get('name') or '').strip():
            warnings.append('Candidate name could not be extracted reliably.')

        education = profile.get('education') if isinstance(profile.get('education'), dict) else {}
        if not any(str(education.get(field_name) or '').strip() for field_name in ('degree', 'field', 'university')):
            warnings.append('Education details are incomplete or missing.')

        experiences = profile.get('experiences') if isinstance(profile.get('experiences'), list) else []
        if not experiences:
            warnings.append('No experience entries were extracted from the CV.')

        skills = profile.get('skills') if isinstance(profile.get('skills'), dict) else {}
        if not skills:
            warnings.append('No skills were extracted from the CV.')

        return warnings

    def _month_from_name(self, month_name):
        return self._MONTH_NAME_TO_NUMBER.get(str(month_name or '').strip().lower(), 0)

    def _duration_to_year_interval(self, duration_text):
        normalized = self._normalize_duration_text(duration_text)
        if not normalized:
            return None

        month_year_matches = list(
            re.finditer(
                r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|'
                r'sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(19|20\d{2})\b',
                normalized.lower(),
            )
        )

        month_year_points = []
        for match in month_year_matches:
            month = self._month_from_name(match.group(1))
            year = int(match.group(2))
            if month and year:
                month_year_points.append((year, month))

        if month_year_points:
            start_year, start_month = month_year_points[0]
            if re.search(r'\b(present|current|now|ongoing)\b', normalized.lower()):
                today = date.today()
                end_year, end_month = today.year, today.month
            else:
                end_year, end_month = month_year_points[-1]

            if (end_year, end_month) < (start_year, start_month):
                start_year, start_month, end_year, end_month = end_year, end_month, start_year, start_month
            return (start_year, start_month, end_year, end_month)

        year_values = [int(year) for year in re.findall(r'\b(?:19|20)\d{2}\b', normalized)]
        if not year_values:
            return None

        start_year = year_values[0]
        end_year = year_values[-1]
        start_month = 1
        end_month = 12

        if re.search(r'\b(present|current|now|ongoing)\b', normalized.lower()):
            today = date.today()
            end_year = today.year
            end_month = today.month

        if (end_year, end_month) < (start_year, start_month):
            start_year, start_month, end_year, end_month = end_year, end_month, start_year, start_month

        return (start_year, start_month, end_year, end_month)

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

        intervals.sort(key=lambda interval: (interval[0], interval[1]))
        merged_intervals = [list(intervals[0])]
        for start_year, start_month, end_year, end_month in intervals[1:]:
            last_start_year, last_start_month, last_end_year, last_end_month = merged_intervals[-1]
            last_end_index = (last_end_year * 12) + last_end_month
            current_start_index = (start_year * 12) + start_month
            current_end_index = (end_year * 12) + end_month
            if current_start_index <= (last_end_index + 1):
                if current_end_index > last_end_index:
                    merged_intervals[-1][2] = end_year
                    merged_intervals[-1][3] = end_month
            else:
                merged_intervals.append([start_year, start_month, end_year, end_month])

        total_years = 0.0
        for start_year, start_month, end_year, end_month in merged_intervals:
            start_index = (start_year * 12) + start_month
            end_index = (end_year * 12) + end_month
            months_span = max(1, end_index - start_index + 1)
            total_years += float(months_span) / 12.0

        return round(total_years, 1)

    def _is_language_skill(self, skill_name):
        return self._canonical_skill_name(skill_name) in self._LANGUAGE_SKILL_NAMES

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

        textual_map = {
            'NATIVE': 'C2',
            'MOTHER TONGUE': 'C2',
            'BILINGUAL': 'C2',
            'FLUENT': 'C1',
            'ADVANCED': 'C1',
            'PROFESSIONAL': 'B2',
            'INTERMEDIATE': 'B1',
            'ELEMENTARY': 'A2',
            'BEGINNER': 'A1',
            'MATERNELLE': 'C2',
            'BILINGUE': 'C2',
            'COURANT': 'C1',
            'PROFESSIONNEL': 'B2',
            'DEBUTANT': 'A1',
        }
        mapped = textual_map.get(level_text)
        if mapped:
            return mapped

        compact = re.sub(r'\s+', '', level_text)
        if compact in self._LANGUAGE_LEVELS:
            return compact

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
            canonical_name = self._canonical_skill_name(normalized_name)
            if not canonical_name:
                continue

            if self._is_language_skill(canonical_name):
                normalized_level = self._normalize_language_skill_level(skill_level)
            else:
                normalized_level = self._normalize_general_skill_level(skill_level)

            if normalized_level:
                normalized_skills[canonical_name] = normalized_level

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
            'extraction_warnings': [],
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
            'Always include spoken languages inside "skills" with CEFR levels when available. '\
            'Use canonical language names when possible: english, french, arabic, spanish, german, italian, portuguese. '\
            'Do not include markdown, comments, or explanations.'
        )

        chunks = self._split_cv_text_chunks(cv_text)
        if not chunks:
            return self._normalize_ai_profile({})

        profiles = []
        for chunk_index, chunk in enumerate(chunks, start=1):
            user_prompt = (
                'Extract CV data from the text below. '\
                'Set "id" to %s. '\
                'If a value is missing, use empty string, empty list, or empty object. '\
                'Always include skills_pertinents for each experience when possible. '\
                'Use string proficiency levels (not numeric). '\
                'Chunk %s/%s of a longer CV.\n\nCV TEXT:\n%s'
            ) % (self.id, chunk_index, len(chunks), chunk)
            ai_payload = self._call_groq_json(system_prompt, user_prompt)
            profiles.append(self._normalize_ai_profile(ai_payload))

        merged = self._merge_profiles(profiles)
        if len(chunks) > 1:
            merged_warnings = list(merged.get('extraction_warnings') or [])
            merged_warnings.append(
                'Long CV parsed in %s chunks. Verify chronology and duplicate entries manually.' % len(chunks)
            )
            merged['extraction_warnings'] = merged_warnings
        return merged

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


    def _normalize_skill_key(self, value):
        normalized = str(value or '').strip().lower()
        if not normalized:
            return ''
        normalized = re.sub(r'[^a-z0-9\s\+#\./-]', ' ', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return normalized

    def _canonical_skill_name(self, value):
        normalized = self._normalize_skill_key(value)
        if not normalized:
            return ''
        normalized = re.sub(r'^(language|langue)\s+', '', normalized).strip()
        normalized = self._SKILL_SYNONYMS.get(normalized, normalized)
        return self._LANGUAGE_SKILL_ALIASES.get(normalized, normalized)


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
        fit_level_map = {
            'Strong Fit': 'Adequation forte',
            'Moderate Fit': 'Adequation moderee',
            'Weak Fit': 'Adequation faible',
            'Adequation forte': 'Adequation forte',
            'Adequation moderee': 'Adequation moderee',
            'Adequation faible': 'Adequation faible',
        }
        fit_level = fit_level_map.get(fit_level, 'Adequation moderee')

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

    def _score_applicant_against_job_with_groq(self, applicant_data, job_data):
        self.ensure_one()
        system_prompt = (
            'Tu es un recruteur senior specialise en evaluation de profils. '
            'Ta mission est de determiner si le candidat correspond au poste, avec une logique stricte, explicable et basee uniquement sur les donnees fournies. '
            'Le matching doit etre STRICTEMENT relatif au poste extrait dans job_data (pas une evaluation generale du candidat). '
            'Traite job_data comme source d autorite pour les exigences: title, skills, min_exp_years, education, major. '
            'Regles de decision: '
            '1) N invente aucune information: si une preuve n existe pas, considere-la comme non prouvee. '
            '2) Fais un matching semantique des competences (synonymes, formulations proches, outils equivalents), sans sur-evaluer. '
            '3) Distingue exigences critiques, exigences importantes et bonus. '
            '4) Penalise fortement l absence d exigences critiques. '
            '5) Base la conclusion sur preuves concretes: experiences, niveau de competences, education, langues, certifications. '
            '6) Si job_data ne contient pas une exigence, n en cree pas. Si une exigence du job est vide/ambigu, mentionne-la dans ambiguities_to_verify. '
            'Ponderation obligatoire (total 100): competences_techniques=40, experience=35, education=15, langues=10. '
            'Interpretation fit_level: Adequation forte si score>=75 sans lacune critique; Adequation moderee si score 50-74 ou incertitudes importantes; Adequation faible si score<50 ou lacunes critiques. '
            'Retourne UNIQUEMENT un objet JSON valide avec EXACTEMENT cette structure:\n'
            '{\n'
            '  "explanation": {\n'
            '    "competences_techniques": "string",\n'
            '    "experience": "string",\n'
            '    "education": "string",\n'
            '    "langues": "string"\n'
            '  },\n'
            '  "score_details": {\n'
            '    "competences_techniques": <int 0..40>,\n'
            '    "experience": <int 0..35>,\n'
            '    "education": <int 0..15>,\n'
            '    "langues": <int 0..10>\n'
            '  },\n'
            '  "matched_skills": ["string"],\n'
            '  "missing_requirements": ["string"],\n'
            '  "bonus_matches": ["string"],\n'
            '  "ai_feedback": {\n'
            '    "fit_level": "Adequation forte|Adequation moderee|Adequation faible",\n'
            '    "summary": "string",\n'
            '    "strengths": ["string"],\n'
            '    "risks": ["string"],\n'
            '    "ambiguities_to_verify": ["string"],\n'
            '    "interview_questions": ["string"],\n'
            '    "recommendation": "Poursuivre|Poursuivre avec prudence|Rejeter"\n'
            '  }\n'
            '}\n'
            'Contraintes strictes: pas de markdown, pas de texte hors JSON, pas de cles supplementaires. '
            'Le score_total est implicite et sera calcule en additionnant les 4 sous-scores.'
        )

        prompt_payload = {
            'candidate': applicant_data,
            'job': job_data,
        }

        user_prompt = (
            'Evalue l adequation du candidat UNIQUEMENT par rapport a CE job extrait (job_data).\n'
            'Objectif: determiner si le candidat peut performer sur ce role precis, pas sur un poste general.\n'
            'Important: job_data est la reference obligatoire des exigences du poste.\n'
            'Instructions d analyse: '\
            '1) Compare candidate.skills et job.skills avec matching semantique et niveau reel. '\
            '2) Verifie experience_years vs job.min_exp_years et la pertinence des experiences pour job.title. '\
            '3) Verifie education du candidat vs job.education et job.major. '\
            '4) Verifie langues requises et niveau probable. '\
            '5) Identifie clairement competences couvertes, manques critiques, manques non bloquants, et bonus. '\
            '6) Mets dans ambiguities_to_verify tout point ambigu, non prouve ou contradictoire. '\
            '7) Recommendation stricte: Rejeter si manques critiques majeurs; Poursuivre avec prudence si fit moyen ou preuves insuffisantes; Poursuivre si fit solide et risques faibles.\n'
            'Base toi uniquement sur ces donnees (aucune hypothese externe).\n'
            'Donnees (JSON):\n%s'
        ) % json.dumps(prompt_payload, ensure_ascii=False)

        try:
            payload = self._call_groq_json(system_prompt, user_prompt, max_tokens=2400)
            payload['status'] = 'done'
            return self._normalize_match_score_payload(payload)
        except Exception as error:
            _logger.warning('Failed to compute AI match: %s', error, exc_info=True)
            raise UserError('Erreur lors du matching AI avec Groq: %s' % error)

    def get_applicant_job_match_data(self):
        self.ensure_one()
        applicant_data = self.get_extracted_applicant_data()
        job_list = self.get_extracted_job_data()
        if not job_list:
            raise UserError('No linked job data found for this applicant.')
        if len(job_list) > 1:
            raise UserError(
                'Multiple job payloads found for this applicant. '
                'Please keep only one target job before running scoring.'
            )

        job_data = job_list[0]
        match_data = self._score_applicant_against_job_with_groq(applicant_data, job_data)
        self.write({
            'score_total': int(match_data.get('score_total') or 0),
            'ai_feedback': self._ai_feedback_to_html(match_data.get('ai_feedback') or {}),
            'applicant_extracted_json': json.dumps(applicant_data or {}, ensure_ascii=False, indent=2),
        })
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
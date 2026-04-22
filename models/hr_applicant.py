import json
import logging
import math
import os
import re
from html import escape
import requests
from odoo import fields, models
from odoo.exceptions import UserError
_logger = logging.getLogger(__name__)

class HrApplicant(models.Model):
    _inherit = 'hr.applicant'
    _GROQ_CHAT_COMPLETIONS_URL = 'https://api.groq.com/openai/v1/chat/completions'
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

    score_total = fields.Integer(string='Score Total', readonly=True, copy=False)
    ai_feedback = fields.Html(string='AI Feedback', readonly=True, copy=False)
    applicant_extracted_json = fields.Text(string='Applicant Extracted JSON', readonly=True, copy=False)
    score_history_ids = fields.One2many(
        'hr.applicant.score.history',
        'applicant_id',
        string='Score History',
        readonly=True,
    )

    # the function button
    def action_run_manual_scoring(self):
        self.ensure_one()
        self.get_applicant_job_match_data()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'hr.applicant',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

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
        return attachment.index_content or ''

    def _is_likely_image_only_pdf(self, attachment):
        if not attachment or not self._is_pdf_attachment(attachment):
            return False
        return not bool(self._get_cv_text(attachment))

    def _verify_cv_is_text_based(self, attachment):
        cv_text = self._get_cv_text(attachment)
        params = self.env['ir.config_parameter'].sudo()
        min_length_value = (params.get_param('scoring_candidates.cv_min_extractable_length') or '').strip()
        min_extractable_length = int(min_length_value) if min_length_value else 200
        if not cv_text:
            if self._is_likely_image_only_pdf(attachment):
                raise UserError(
                    'The selected CV appears to be image-based (scanned PDF), so no readable text was found. '
                    'Please upload a searchable PDF or run OCR first.'
                )
            raise UserError(
                'No extracted text found in the PDF attachment. '
                'Use a searchable PDF or enable attachment indexing in Odoo.'
            )

        if len(cv_text) < int(min_extractable_length):
            raise UserError(
                'CV text is too short to extract meaningful data (%d characters found, minimum is %d). '
                'The PDF may be corrupted, contain only a header, or indexing may be incomplete.'
                % (len(cv_text), int(min_extractable_length))
            )

        return cv_text

    def _get_groq_configuration(self, stage='general'):
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

        model_keys_by_stage = {
            'extraction': ('scoring_candidates.groq_model_extraction',),
            'comparison': ('scoring_candidates.groq_model_comparison',),
        }
        model = ''
        for key_name in model_keys_by_stage.get(stage, ()):
            key_value = (params.get_param(key_name) or '').strip()
            if key_value:
                model = key_value
                break

        if not model:
            model = params.get_param('scoring_candidates.groq_model')

        if not api_key:
            raise UserError(
                'Missing Groq API key.\n'
                'Set one of these system parameters:\n'
                '- scoring_candidates.groq_api_key\n'
                '- groq_api_key\n'
                'Or set environment variable GROQ_API_KEY and restart Odoo.'
            )
        return api_key, model

    def _call_groq_json(self, system_prompt, user_prompt, max_tokens=3600, stage='general'):
        self.ensure_one()
        api_key, model = self._get_groq_configuration(stage=stage)
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

        try:
            response_payload = response.json()
        except ValueError as json_error:
            raise UserError(
                'Groq returned a non-JSON HTTP response (status %s). Raw: %s'
                % (response.status_code, response.text[:300])
            ) from json_error

        if 'error' in response_payload:
            api_error = response_payload['error']
            raise UserError(
                'Groq API error: [%s] %s'
                % (api_error.get('type', 'unknown'), api_error.get('message', str(api_error)))
            )

        choices = response_payload.get('choices') or []
        if not choices:
            raise UserError(
                'Groq response did not contain choices. Full response: %s'
                % str(response_payload)[:400]
            )

        message = choices[0].get('message') or {}
        content = message.get('content')
        if not content:
            finish_reason = choices[0].get('finish_reason', 'unknown')
            raise UserError(
                'Groq response content is empty (finish_reason: %s). '
                'The model may have hit max_tokens or been interrupted.'
                % finish_reason
            )

        finish_reason = choices[0].get('finish_reason', '')
        if finish_reason == 'length':
            _logger.warning(
                'Groq response was truncated at max_tokens. '
                'Consider increasing max_tokens or reducing CV chunk size.'
            )

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

    def _default_extracted_profile(self):
        return {
            'id': self.id,
            'name': self.partner_name or '',
            'education': {'degree': '', 'field': '', 'university': '', 'date': ''},
            'experiences': [],
            'experience_years': 0.0,
            'certification': [],
            'skills': {},
            'extraction_warnings': [],
            'chunk_count': 1,
        }

    def _get_cv_extraction_runtime_params(self, cv_text):
        text_length = len(str(cv_text or ''))
        params = self.env['ir.config_parameter'].sudo()

        def _int_param(key, default_value):
            raw_value = (params.get_param(key) or '').strip()
            if not raw_value:
                return int(default_value)
            try:
                return int(raw_value)
            except (TypeError, ValueError):
                return int(default_value)

        max_chunks = _int_param('scoring_candidates.cv_extraction_max_chunks', 0)

        # Dynamic tuning can be disabled from system parameters when fixed sizing is preferred.
        dynamic_flag = (params.get_param('scoring_candidates.cv_extraction_dynamic') or '1').strip().lower()
        dynamic_enabled = dynamic_flag not in ('0', 'false', 'no', 'off')

        fixed_chunk_size = _int_param('scoring_candidates.cv_extraction_chunk_size', 9000)
        fixed_overlap = _int_param('scoring_candidates.cv_extraction_chunk_overlap', 600)
        fixed_response_tokens = _int_param('scoring_candidates.cv_extraction_max_response_tokens', 5200)

        dynamic_chunk_size_min = _int_param('scoring_candidates.cv_dynamic_chunk_size_min', 6000)
        dynamic_chunk_size_max = _int_param('scoring_candidates.cv_dynamic_chunk_size_max', 10000)
        dynamic_overlap_min = _int_param('scoring_candidates.cv_dynamic_overlap_min', 450)
        dynamic_overlap_max = _int_param('scoring_candidates.cv_dynamic_overlap_max', 700)
        dynamic_response_tokens_min = _int_param('scoring_candidates.cv_dynamic_response_tokens_min', 3200)
        dynamic_response_tokens_max = _int_param('scoring_candidates.cv_dynamic_response_tokens_max', 5200)

        short_threshold = _int_param('scoring_candidates.cv_dynamic_short_threshold', 10000)
        medium_threshold = _int_param('scoring_candidates.cv_dynamic_medium_threshold', 25000)

        if not dynamic_enabled:
            return {
                'chunk_size': max(1, fixed_chunk_size),
                'overlap': max(0, fixed_overlap),
                'max_chunks': max_chunks,
                'max_response_tokens': max(256, fixed_response_tokens),
            }

        if text_length <= short_threshold:
            chunk_size = dynamic_chunk_size_min
            overlap = dynamic_overlap_min
            max_response_tokens = dynamic_response_tokens_min
        elif text_length <= medium_threshold:
            chunk_size = int((dynamic_chunk_size_min + dynamic_chunk_size_max) / 2)
            overlap = int((dynamic_overlap_min + dynamic_overlap_max) / 2)
            max_response_tokens = int((dynamic_response_tokens_min + dynamic_response_tokens_max) / 2)
        else:
            chunk_size = dynamic_chunk_size_max
            overlap = dynamic_overlap_max
            max_response_tokens = dynamic_response_tokens_max

        return {
            'chunk_size': max(1, chunk_size),
            'overlap': max(0, overlap),
            'max_chunks': max_chunks,
            'max_response_tokens': max(256, max_response_tokens),
        }

    def _split_cv_chunks(self, cv_text, runtime_params=None):
        raw_text = str(cv_text or '')
        if not raw_text:
            return []

        runtime = runtime_params or self._get_cv_extraction_runtime_params(raw_text)
        chunk_size = int(runtime.get('chunk_size') or 9000)
        overlap = int(runtime.get('overlap') or 600)
        max_chunks = int(runtime.get('max_chunks') if runtime.get('max_chunks') is not None else 0)
        if chunk_size <= 0:
            return [raw_text]

        overlap = max(0, min(overlap, max(0, chunk_size - 1)))
        chunks = []
        start = 0
        unlimited_chunks = max_chunks <= 0
        while start < len(raw_text) and (unlimited_chunks or len(chunks) < max_chunks):
            end = min(start + chunk_size, len(raw_text))
            chunks.append(raw_text[start:end])
            if end >= len(raw_text):
                break
            start = max(start + 1, end - overlap)

        return chunks or [raw_text]

    def _estimate_cv_chunk_count(self, text_length, runtime_params=None):
        if text_length <= 0:
            return 0
        runtime = runtime_params or {}
        chunk_size = int(runtime.get('chunk_size') or 9000)
        overlap = int(runtime.get('overlap') or 600)
        if chunk_size <= 0:
            return 1
        overlap = max(0, min(overlap, max(0, chunk_size - 1)))
        step = max(1, chunk_size - overlap)
        if text_length <= chunk_size:
            return 1
        return int(math.ceil(float(text_length - chunk_size) / float(step))) + 1

    def _merge_extraction_payloads(self, payloads, extraction_warnings=None):
        merged = self._default_extracted_profile()
        merged['extraction_warnings'] = list(extraction_warnings or [])

        seen_experiences = set()
        seen_certifications = set()

        for payload in payloads:
            if not isinstance(payload, dict):
                continue

            if not str(merged.get('name') or '').strip():
                merged['name'] = str(payload.get('name') or '').strip() or merged['name']

            education = payload.get('education') if isinstance(payload.get('education'), dict) else {}
            merged_education = merged.get('education') if isinstance(merged.get('education'), dict) else {}
            for field_name in ('degree', 'field', 'university', 'date'):
                if not str(merged_education.get(field_name) or '').strip():
                    merged_education[field_name] = str(education.get(field_name) or '').strip()
            merged['education'] = merged_education

            for experience in (payload.get('experiences') or []):
                if not isinstance(experience, dict):
                    continue
                key = (
                    str(experience.get('title') or '').strip().lower(),
                    str(experience.get('company') or '').strip().lower(),
                    str(experience.get('duration') or '').strip().lower(),
                )
                if key in seen_experiences:
                    continue
                seen_experiences.add(key)
                merged['experiences'].append(experience)

            for certification in (payload.get('certification') or []):
                text = str(certification or '').strip()
                if not text:
                    continue
                fingerprint = text.lower()
                if fingerprint in seen_certifications:
                    continue
                seen_certifications.add(fingerprint)
                merged['certification'].append(text)

            for skill_name, skill_level in (payload.get('skills') or {}).items():
                if not str(skill_name or '').strip():
                    continue
                merged['skills'][str(skill_name).strip()] = str(skill_level).strip()

            try:
                merged['experience_years'] = max(
                    float(merged.get('experience_years') or 0.0),
                    float(payload.get('experience_years') or 0.0),
                )
            except (TypeError, ValueError):
                pass

            for warning in (payload.get('extraction_warnings') or []):
                warning_text = str(warning).strip()
                if warning_text and warning_text not in merged['extraction_warnings']:
                    merged['extraction_warnings'].append(warning_text)

        return merged

    def _extract_profile_with_groq(self, cv_text):
        self.ensure_one()
        raw_cv_text = str(cv_text or '')
        runtime_params = self._get_cv_extraction_runtime_params(raw_cv_text)

        system_prompt = (
            'You are an Expert CV Parser specialized in extracting structured information from raw CV text. '\
            'Your task is to read unstructured CV text and extract relevant information into a clean, structured JSON format that strictly follows the provided schema.'\
            'Return ONLY valid JSON with this exact top-level structure: '\
            '{"id": int, "name": str, "education": {"degree": str, "field": str, "university": str, "date": str}, '\
            '"experiences": [{"title": str, "company": str, "duration": str, "general_context": str, "project_topic": str, "responsibilities": str, "work_done": [str], "results_obtained": [str], "skills_pertinents": {'\
            '"Soft Skills": [str], "Logiciels": [str], "Langages de programmation": [str], "Matériels": [str], '\
            '"Méthodes": [str], "Normes et protocoles": [str], "Systèmes": [str], "Technologies": [str], "Marketing": [str]}}], '\
            '"experience_years": float, '\
            '"certification": [str], '\
            '"skills": {"skill_name": str_level}}. '\
            'For each experience, field definitions are strict: '\
            '- general_context = organizational/business context. '\
            '- project_topic = main project/product/topic. '\
            '- responsibilities = ownership/accountabilities. '\
            '- work_done = list of concrete actions executed. '\
            '- results_obtained = list of explicit outcomes/KPIs/impact. '\
            'Use only evidence present in CV text. '\
            'Language rule: write all extracted textual content strictly in French, including all narrative fields. '\
            'If source text is in another language, translate faithfully into natural professional French without losing meaning. '\
            'Do not infer results from responsibilities. '\
            'Narrative richness rules: for each experience and each of the 5 narrative fields, when evidence exists, write 2 to 4 complete sentences and at least 30 words. '\
            'Each field must be specific and non-generic, grounded in concrete facts from the same experience. '\
            'general_context must mention business/domain context and mission scope. '\
            'project_topic must mention project objective and functional focus. '\
            'responsibilities must describe ownership, decisions, and accountability perimeter. '\
            'work_done must detail concrete actions, tools/methods, and execution scope. '\
            'results_obtained must describe factual outcomes and quantified impact when present in CV text. '\
            'Avoid short vague fillers like "participation a" without details. '\
            'Keep task items concise and deduplicated. '\
            'In "skills", prioritize languages and skills explicitly listed in the CV skills section. '\
            'For technical and soft skills, use exactly one of: Beginner, Elementary, Intermediate, Advanced, Expert. '\
            'For language skills, use exactly one of: A1, A2, B1, B2, C1, C2. '\
            'Always include spoken languages inside "skills" with CEFR levels when available. '\
            'Use canonical language names when possible: francais, anglais, arabe, espagnol, allemand, italien, portugais. '\
            'Do not include markdown, comments, or explanations.'
        )

        chunks = self._split_cv_chunks(raw_cv_text, runtime_params=runtime_params)
        payloads = []
        extraction_warnings = []
        expected_chunk_count = self._estimate_cv_chunk_count(len(raw_cv_text), runtime_params=runtime_params)
        if expected_chunk_count and len(chunks) < expected_chunk_count:
            extraction_warnings.append(
                'CV text was truncated during chunking (%s/%s chunks processed). '
                'Increase system parameter scoring_candidates.cv_extraction_max_chunks.'
                % (len(chunks), expected_chunk_count)
            )

        for chunk_index, chunk_text in enumerate(chunks, start=1):
            user_prompt = (
                'Extract CV data from the text below. '
                'Set "id" to %s. '
                'If a value is missing, use empty string, empty list, or empty object. '
                'Identify experiences first, then extract details. '
                'Write all text outputs in French only (no English): title, company, and all narrative fields must be in French. '
                'If the CV uses another language, translate extracted content to French while preserving factual meaning. '
                'For each experience, always include: general_context, project_topic, responsibilities, work_done, results_obtained. '
                'Return work_done and results_obtained as arrays of strings (not a single string). '
                'Always include skills_pertinents as a dictionary of categories for each experience. '
                'In top-level skills, include languages and explicit skills-section items first. '
                'Use string proficiency levels (not numeric). '
                'This is chunk %s/%s of the same CV.\n\n'
                'CV TEXT:\n%s'
            ) % (self.id, chunk_index, len(chunks), chunk_text)

            try:
                payload = self._call_groq_json(
                    system_prompt,
                    user_prompt,
                    max_tokens=int(runtime_params.get('max_response_tokens') or 5200),
                    stage='extraction',
                )
            except Exception as error:
                _logger.warning('CV extraction failed on chunk %s/%s: %s', chunk_index, len(chunks), error)
                extraction_warnings.append('Chunk %s/%s failed: %s' % (chunk_index, len(chunks), error))
                continue

            if not isinstance(payload, dict):
                extraction_warnings.append('Chunk %s/%s returned non-object JSON.' % (chunk_index, len(chunks)))
                continue

            payload.setdefault('id', self.id)
            payload.setdefault('name', self.partner_name or '')
            payload.setdefault('education', {'degree': '', 'field': '', 'university': '', 'date': ''})
            payload.setdefault('experiences', [])
            payload.setdefault('experience_years', 0.0)
            payload.setdefault('certification', [])
            payload.setdefault('skills', {})
            payload.setdefault('extraction_warnings', [])
            payloads.append(payload)

        if not payloads:
            default_payload = self._default_extracted_profile()
            default_payload['extraction_warnings'] = extraction_warnings or ['CV extraction failed for all chunks.']
            return default_payload

        merged_payload = self._merge_extraction_payloads(payloads, extraction_warnings=extraction_warnings)
        merged_payload['chunk_count'] = len(chunks)
        if len(chunks) > 1 and not merged_payload.get('extraction_warnings'):
            merged_payload['extraction_warnings'] = ['Long CV processed in %s chunks.' % len(chunks)]
        elif len(chunks) > 1:
            merged_payload['extraction_warnings'].append('Long CV processed in %s chunks.' % len(chunks))
        return merged_payload

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

    def _normalize_job_skills_for_scoring(self, job_data):
        """
        Applies the same canonical skill normalization to job-side skill lists
        so the LLM sees reconciled names on both sides (e.g. NodeJS == node.js).
        Returns a new job_data dict with normalized skill entries.
        """
        if not isinstance(job_data, dict):
            return job_data

        normalized_job = dict(job_data)

        for skill_list_key in ('required_skills', 'optional_skills', 'skills', 'competences'):
            raw_list = normalized_job.get(skill_list_key)
            if not isinstance(raw_list, list):
                continue
            normalized_job[skill_list_key] = [
                self._canonical_skill_name(skill) or skill
                for skill in raw_list
                if str(skill).strip()
            ]

        for skill_map_key in ('skills_required', 'skills_map'):
            raw_map = normalized_job.get(skill_map_key)
            if not isinstance(raw_map, dict):
                continue
            normalized_job[skill_map_key] = {
                (self._canonical_skill_name(key) or key): value
                for key, value in raw_map.items()
                if str(key).strip()
            }

        return normalized_job

    def _score_applicant_against_job_with_groq(self, applicant_data, job_data):
        self.ensure_one()
        normalized_job_data = self._normalize_job_skills_for_scoring(job_data)
        system_prompt = (
            'Framework: PICCO\n\n'
            '[P] Persona\n'
            'Tu es un ATS IA et recruteur technique senior.\n\n'
            '[I] Intent\n'
            'Evaluer strictement un candidat par rapport a un poste, uniquement a partir des donnees JSON fournies.\n\n'
            '[C] Context\n'
            'L evaluation doit etre strictement relative au poste cible (job) et a ses criteres.\n\n'
            '[C] Constraints\n'
            '1) Zero hallucination: aucune information inventee, aucune hypothese externe.\n'
            '2) Autorite du job: l evaluation doit etre strictement relative a job.\n'
            '3) Langue de sortie: tout le contenu textuel final doit etre en francais.\n'
            '4) Si job.education est vide -> education=15. Si exigences de langues absentes -> langues=10.\n'
            '5) Penalite recence: competence coeur non utilisee depuis >24 mois -> -15% sur sa contribution.\n'
            '6) Densite experience: si poste Senior/Lead/Architect/Manager et titres candidat uniquement Junior/Intern/Trainee -> -10 points sur experience.\n'
            '7) Stabilite: tenure moyenne <12 mois -> ajouter risque de job hopping dans risks et dans ambiguities_to_verify.\n'
            '8) Proximite d outil: outil exact absent mais concurrent direct maitrise -> 50% du credit, et mentionner "Equivalent tool mastered - verification required" dans ambiguities_to_verify.\n'
            '9) Alignement education: domaine fortement non aligne (ex: Biologie pour Data Science) -> education=0, sauf compensation partielle par certifications specialisees pertinentes.\n'
            '10) Interdiction d evaluation generale: ne jamais commenter le candidat en dehors des exigences du poste (pas de jugement global de personnalite, potentiel, ou valeur generale).\n'
            '11) Tracabilite obligatoire: chaque item de strengths/risks/missing_requirements/bonus_matches doit etre rattache explicitement a un critere du poste (competence, responsabilite, niveau, domaine, langue, outillage, anciennete).\n'
            '12) Neutralite hors-perimetre: les informations du candidat non demandees par job doivent etre ignorees, sans bonus ni malus.\n'
            '13) Priorite aux gaps critiques: un manque critique requis par job doit peser plus qu un bonus non requis.\n'
            '14) Bareme total 100:\n'
            '- competences_techniques: 0..40\n'
            '- experience: 0..35\n'
            '- education: 0..15\n'
            '- langues: 0..10\n'
            '15) Fit level:\n'
            '- Adequation forte: score >= 75 et pas de manque critique bloquant\n'
            '- Adequation moderee: score 50..74 ou incertitudes importantes\n'
            '- Adequation faible: score < 50 ou manque critique bloquant\n\n'
            '[O] Output\n'
            'Retourne uniquement un JSON valide et rien d autre, avec cette structure exacte:\n'
            '{\n'
            '  "explanation": {\n'
            '    "competences_techniques": "Raisonnement factuel de la note",\n'
            '    "experience": "Comparaison stricte des années et de la pertinence",\n'
            '    "education": "Analyse du diplôme",\n'
            '    "langues": "Analyse du niveau de langue"\n'
            '  },\n'
            '  "score_details": {\n'
            '    "competences_techniques": <int 0..40>,\n'
            '    "experience": <int 0..35>,\n'
            '    "education": <int 0..15>,\n'
            '    "langues": <int 0..10>\n'
            '  },\n'
            '  "matched_skills": ["string"],\n'
            '  "missing_requirements": ["string (Omission critique uniquement)"],\n'
            '  "bonus_matches": ["string"],\n'
            '  "ai_feedback": {\n'
            '    "fit_level": "Adequation forte|Adequation moderee|Adequation faible",\n'
            '    "summary": "Résumé exécutif axe uniquement sur l adequation au poste et les ecarts critiques",\n'
            '    "strengths": ["string"],\n'
            '    "risks": ["string"],\n'
            '    "ambiguities_to_verify": ["string (ex: Chevauchement de dates, niveau réel de l\'outil X)"],\n'
            '    "recommendation": "Poursuivre|Poursuivre avec prudence|Rejeter"\n'
            '  }\n'
            '}'
        )

        prompt_payload = {
            'candidate': applicant_data,
            'job': normalized_job_data,
        }

        user_prompt = (
            'Framework: PICCO\n\n'
            '[P] Persona\n'
            'Tu agis comme evaluateur ATS technique strict.\n\n'
            '[I] Intent\n'
            'Produire une evaluation exploitable pour la decision RH sur ce poste uniquement.\n\n'
            '[C] Context\n'
            'Analyse ce candidat contre ce poste en appliquant strictement les regles du system prompt.\n\n'
            '[C] Constraints\n'
            '1) Comparer skills, experience, education et langues.\n'
            '2) Appliquer les ajustements (recence, seniorite, stabilite, equivalence outils, alignement education).\n'
            '3) Calculer les 4 sous-scores dans leurs bornes.\n'
            '4) Deduire fit_level et recommendation.\n'
            '5) Produire des risques et points a verifier actionnables.\n'
            '6) Chaque phrase de feedback doit citer un lien explicite au poste (requis/souhaite/non requis).\n'
            '7) Ignorer totalement les elements du candidat hors perimetre du poste.\n'
            '8) Ne pas produire de conclusion generale: uniquement une conclusion d adequation au poste cible.\n'
            '9) Format attendu par item (obligatoire quand applicable): "[Critere job] -> [Evidence candidat] -> [Impact sur score]".\n\n'
            '[O] Output\n'
            'Respecte strictement le schema JSON exige par le system prompt.\n\n'
            'Donnees JSON a comparer:\n%s'
        ) % json.dumps(prompt_payload, ensure_ascii=False)

        try:
            payload = self._call_groq_json(
                system_prompt,
                user_prompt,
                max_tokens=2400,
                stage='comparison',
            )
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
        _, groq_model = self._get_groq_configuration(stage='comparison')
        warnings = applicant_data.get('extraction_warnings') or []
        self.write({
            'score_total': int(match_data.get('score_total') or 0),
            'ai_feedback': self._ai_feedback_to_html(match_data.get('ai_feedback') or {}),
            'applicant_extracted_json': json.dumps(applicant_data or {}, ensure_ascii=False, indent=2),
        })
        self.env['hr.applicant.score.history'].create({
            'applicant_id': self.id,
            'score_total': int(match_data.get('score_total') or 0),
            'ai_feedback': self._ai_feedback_to_html(match_data.get('ai_feedback') or {}),
            'extracted_json': json.dumps(applicant_data or {}, ensure_ascii=False, indent=2),
            'groq_model': groq_model,
            'chunk_count': int(applicant_data.get('chunk_count') or 0),
            'extraction_warnings': '\n'.join(warnings) if warnings else '',
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

    def get_applicant_extracted_payload(self):
        self.ensure_one()
        raw_payload = self.applicant_extracted_json or '{}'
        if isinstance(raw_payload, dict):
            return raw_payload
        try:
            payload = json.loads(raw_payload)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

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


class HrApplicantScoreHistory(models.Model):
    _name = 'hr.applicant.score.history'
    _description = 'Applicant Score History'
    _order = 'scored_at desc'

    applicant_id = fields.Many2one('hr.applicant', string='Applicant', required=True, ondelete='cascade')
    scored_at = fields.Datetime(string='Scored At', default=fields.Datetime.now, readonly=True)
    score_total = fields.Integer(string='Score Total', readonly=True)
    ai_feedback = fields.Html(string='AI Feedback Snapshot', readonly=True)
    extracted_json = fields.Text(string='Extracted JSON Snapshot', readonly=True)
    groq_model = fields.Char(string='Model Used', readonly=True)
    chunk_count = fields.Integer(string='Chunks Processed', readonly=True)
    extraction_warnings = fields.Text(string='Extraction Warnings', readonly=True)
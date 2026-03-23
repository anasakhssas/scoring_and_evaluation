import json
import os
import re

import requests

from odoo import models
from odoo.exceptions import UserError


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'
    _GROQ_CHAT_COMPLETIONS_URL = 'https://api.groq.com/openai/v1/chat/completions'

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
            return self.env['ir.attachment']

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
            normalized_experiences.append({
                'title': experience.get('title') or '',
                'company': experience.get('company') or '',
                'duration': self._normalize_duration_text(experience.get('duration')),
                'tasks': [str(task).strip() for task in tasks if str(task).strip()],
            })

        normalized_skills = {}
        for skill_name, skill_level in skills.items():
            if not skill_name:
                continue
            try:
                normalized_level = int(skill_level)
            except (TypeError, ValueError):
                continue
            normalized_skills[str(skill_name).strip()] = max(1, min(5, normalized_level))

        return {
            'id': self.id,
            'name': payload.get('name') or self.partner_name or '',
            'education': {
                'degree': education.get('degree') or '',
                'field': education.get('field') or '',
                'university': education.get('university') or '',
            },
            'experiences': normalized_experiences,
            'skills': normalized_skills,
        }

    def _extract_profile_with_groq(self, cv_text):
        self.ensure_one()
        api_key, model = self._get_groq_configuration()

        system_prompt = (
            'You are an expert CV parser. '\
            'Return ONLY valid JSON with this exact top-level structure: '\
            '{"id": int, "name": str, "education": {"degree": str, "field": str, "university": str}, '\
            '"experiences": [{"title": str, "company": str, "duration": str, "tasks": [str]}], '\
            '"skills": {"skill_name": int_1_to_5}}. '\
            'Do not include markdown, comments, or explanations.'
        )
        user_prompt = (
            'Extract CV data from the text below. '\
            'Set "id" to %s. '\
            'If a value is missing, use empty string, empty list, or empty object. '\
            'Keep skills levels between 1 and 5.\n\nCV TEXT:\n%s'
        ) % (self.id, cv_text[:16000])

        request_payload = {
            'model': model,
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'response_format': {'type': 'json_object'},
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
                timeout=90,
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

        ai_payload = self._extract_json_from_llm_output(content)
        return self._normalize_ai_profile(ai_payload)

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
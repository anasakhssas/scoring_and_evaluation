import json
import logging
import math
import os
import re
import unicodedata
from html import escape
from datetime import date

import requests

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'
    _GROQ_CHAT_COMPLETIONS_URL = 'https://api.groq.com/openai/v1/chat/completions'
    _CV_MIN_EXTRACTABLE_LENGTH = 200
    _CV_EXTRACTION_CHUNK_SIZE = 16000
    _CV_EXTRACTION_CHUNK_OVERLAP = 1000
    _CV_EXTRACTION_MAX_CHUNKS = 3
    _CV_EXTRACTION_MIN_RESPONSE_TOKENS = 2600
    _CV_EXTRACTION_MAX_RESPONSE_TOKENS = 5200
    _CV_EXTRACTION_MAX_HINT_SKILLS = 25
    _GOOD_SKILL_MIN_SCORE5 = 4
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
        'arabe', 'francais', 'anglais', 'espagnol', 'allemand', 'italien', 'portugais',
        'mandarin', 'chinois', 'japonais', 'coreen', 'russe', 'neerlandais', 'turc',
    }
    _LANGUAGE_SKILL_ALIASES = {
        'english': 'anglais',
        'french': 'francais',
        'spanish': 'espagnol',
        'german': 'allemand',
        'italian': 'italien',
        'portuguese': 'portugais',
        'arabic': 'arabe',
        'chinese': 'chinois',
        'mandarin chinese': 'mandarin',
        'japanese': 'japonais',
        'korean': 'coreen',
        'russian': 'russe',
        'dutch': 'neerlandais',
        'turkish': 'turc',
        'langue anglaise': 'anglais',
        'langue francaise': 'francais',
        'french language': 'francais',
        'english language': 'anglais',
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
        'janv': 1, 'janvier': 1,
        'feb': 2, 'february': 2,
        'fev': 2, 'fevr': 2, 'fevrier': 2,
        'mar': 3, 'march': 3,
        'mars': 3,
        'apr': 4, 'april': 4,
        'avr': 4, 'avril': 4,
        'may': 5,
        'mai': 5,
        'jun': 6, 'june': 6,
        'juin': 6,
        'jul': 7, 'july': 7,
        'juil': 7, 'juillet': 7,
        'aug': 8, 'august': 8,
        'aout': 8,
        'sep': 9, 'sept': 9, 'september': 9,
        'septembre': 9,
        'oct': 10, 'october': 10,
        'octobre': 10,
        'nov': 11, 'november': 11,
        'novembre': 11,
        'dec': 12, 'december': 12,
        'decembre': 12,
    }
    _SKILL_PERTINENT_CATEGORIES = (
        'Soft Skills',
        'Logiciels',
        'Langages de programmation',
        'Matériels',
        'Méthodes',
        'Normes et protocoles',
        'Systèmes',
        'Technologies',
        'Marketing',
    )
    _SKILL_CATEGORY_ALIASES = {
        'soft skills': 'Soft Skills',
        'softskills': 'Soft Skills',
        'logiciels': 'Logiciels',
        'software': 'Logiciels',
        'langages de programmation': 'Langages de programmation',
        'langages programmation': 'Langages de programmation',
        'programming languages': 'Langages de programmation',
        'langages': 'Langages de programmation',
        'materiels': 'Matériels',
        'materiel': 'Matériels',
        'hardware': 'Matériels',
        'methodes': 'Méthodes',
        'methodologies': 'Méthodes',
        'methodology': 'Méthodes',
        'normes et protocoles': 'Normes et protocoles',
        'normes protocoles': 'Normes et protocoles',
        'standards and protocols': 'Normes et protocoles',
        'systemes': 'Systèmes',
        'systeme': 'Systèmes',
        'systems': 'Systèmes',
        'technologies': 'Technologies',
        'marketing': 'Marketing',
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

        if len(cv_text) < int(self._CV_MIN_EXTRACTABLE_LENGTH):
            raise UserError(
                'CV text is too short to extract meaningful data (%d characters found, minimum is %d). '
                'The PDF may be corrupted, contain only a header, or indexing may be incomplete.'
                % (len(cv_text), int(self._CV_MIN_EXTRACTABLE_LENGTH))
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
            model = (
                (params.get_param('scoring_candidates.groq_model') or '').strip()
                or (params.get_param('groq_model') or '').strip()
                or 'openai/gpt-oss-120b'
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

    def _normalize_duration_text(self, duration_text):
        normalized = str(duration_text or '').strip()
        if not normalized:
            return ''

        normalized = normalized.replace('—', '-').replace('–', '-')
        normalized = re.sub(r'\s*-\s*', ' - ', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return normalized

    def _is_cv_section_heading(self, line_text):
        line = str(line_text or '').strip()
        if not line or len(line) > 100:
            return False

        normalized = ''.join(
            character
            for character in unicodedata.normalize('NFD', line.lower())
            if unicodedata.category(character) != 'Mn'
        )
        normalized = re.sub(r'[^a-z0-9\s]', ' ', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        if not normalized:
            return False

        section_markers = {
            'profil', 'profil professionnel', 'profile', 'summary', 'executive summary',
            'resume', 'synthese', 'synthese professionnelle', 'a propos', 'about me',
            'objectif', 'objectifs', 'objective',
            'experience', 'experiences', 'professional experience', 'work experience',
            'experience professionnelle', 'experiences professionnelles',
            'parcours professionnel', 'parcours', 'carriere', 'career',
            'historique professionnel', 'postes occupes', 'emplois',
            'activites professionnelles', 'activite professionnelle',
            'missions', 'mission', 'realisation', 'realisations',
            'formation', 'formations', 'education', 'etudes', 'scolarite',
            'diplomes', 'diplome', 'cursus', 'parcours academique',
            'academic background', 'background academique',
            'competences', 'competences techniques', 'competences cles',
            'skills', 'technical skills', 'core competencies', 'key skills',
            'savoir faire', 'savoir-faire', 'expertises', 'expertise',
            'competences informatiques', 'outils', 'technologies',
            'langues', 'languages', 'langue', 'language skills', 'competences linguistiques',
            'certification', 'certifications', 'certificats', 'certificat',
            'licences', 'licence', 'accreditations',
            'projets', 'projects', 'projet', 'project', 'realisations projets',
            'projets realises', 'projets significatifs',
            'publications', 'publication', 'contributions', 'open source',
            'references', 'reference', 'recommandations',
            'centres d interet', 'loisirs', 'interests', 'hobbies',
            'activites extra professionnelles', 'activites associatives',
            'bénévolat', 'benevolat', 'volunteer',
        }

        if normalized in section_markers:
            return True

        for marker in section_markers:
            if normalized.startswith(marker + ' ') or normalized.startswith(marker + ':'):
                return True

        stripped_original = line.strip().rstrip(':').strip()
        if (
            stripped_original == stripped_original.upper()
            and len(stripped_original) >= 4
            and len(stripped_original) <= 60
            and not any(char.isdigit() for char in stripped_original)
        ):
            return True

        return False

    def _split_cv_into_sections(self, cv_text):
        lines = str(cv_text or '').splitlines()
        if not lines:
            return []

        sections = []
        current_lines = []
        for line in lines:
            if self._is_cv_section_heading(line) and current_lines:
                section_text = '\n'.join(current_lines).strip()
                if section_text:
                    sections.append(section_text)
                current_lines = [line]
                continue

            current_lines.append(line)

        if current_lines:
            section_text = '\n'.join(current_lines).strip()
            if section_text:
                sections.append(section_text)

        return sections

    def _split_cv_text_chunks(self, cv_text):
        normalized = str(cv_text or '').strip()
        if not normalized:
            return []

        chunk_size = int(self._CV_EXTRACTION_CHUNK_SIZE)
        overlap = int(self._CV_EXTRACTION_CHUNK_OVERLAP)
        base_max_chunks = int(self._CV_EXTRACTION_MAX_CHUNKS)
        if chunk_size <= 0:
            return [normalized]

        overlap = max(0, min(overlap, max(0, chunk_size - 1)))
        step = max(1, chunk_size - overlap)
        estimated_chunks = max(1, int(math.ceil(float(len(normalized)) / float(step))))
        max_chunks = max(base_max_chunks, min(12, estimated_chunks))

        sections = self._split_cv_into_sections(normalized) or [normalized]

        chunks = []
        current_chunk = ''
        for section in sections:
            section_text = str(section or '').strip()
            if not section_text:
                continue

            candidate_chunk = section_text if not current_chunk else '%s\n\n%s' % (current_chunk, section_text)
            if len(candidate_chunk) <= chunk_size:
                current_chunk = candidate_chunk
                continue

            if current_chunk:
                chunks.append(current_chunk)
                if len(chunks) >= max_chunks:
                    break
                current_chunk = ''

            if len(section_text) <= chunk_size:
                current_chunk = section_text
                continue

            start = 0
            while start < len(section_text) and len(chunks) < max_chunks:
                hard_end = min(start + chunk_size, len(section_text))
                end = hard_end
                if hard_end < len(section_text):
                    min_boundary = start + int(chunk_size * 0.55)
                    newline_end = section_text.rfind('\n', min_boundary, hard_end)
                    sentence_end = section_text.rfind('. ', min_boundary, hard_end)
                    if newline_end > start:
                        end = newline_end + 1
                    elif sentence_end > start:
                        end = sentence_end + 1

                piece = section_text[start:end].strip()
                if piece:
                    chunks.append(piece)
                if end >= len(section_text):
                    break
                start = max(start + 1, end - overlap)

        if current_chunk and len(chunks) < max_chunks:
            chunks.append(current_chunk)

        if not chunks:
            return [normalized]

        return chunks[:max_chunks]

    def _normalize_cv_text_for_extraction(self, cv_text):
        normalized = str(cv_text or '')
        if not normalized:
            return ''

        normalized = normalized.replace('\r\n', '\n').replace('\r', '\n')
        normalized = normalized.replace('•', '- ').replace('◦', '- ').replace('▪', '- ')
        normalized = re.sub(r'([A-Za-z])-\n([A-Za-z])', r'\1\2', normalized)

        cleaned_lines = []
        for line in normalized.split('\n'):
            compact = re.sub(r'\s+', ' ', line).strip()
            if re.match(r'^(page\s+\d+\s*/\s*\d+|page\s+\d+)$', compact.lower()):
                continue
            cleaned_lines.append(compact)

        normalized = '\n'.join(cleaned_lines)
        normalized = re.sub(r'\n{3,}', '\n\n', normalized)
        return normalized.strip()

    def _extract_name_hint_from_cv(self, cv_text):
        if not cv_text:
            return ''

        lines = [re.sub(r'\s+', ' ', line).strip() for line in str(cv_text).splitlines() if str(line).strip()]
        candidate_lines = lines[:12]
        for line in candidate_lines:
            if len(line) < 4 or len(line) > 70:
                continue
            if any(token in line.lower() for token in ('@', 'http', 'www', 'linkedin', 'github', 'tel', 'phone')):
                continue
            if any(char.isdigit() for char in line):
                continue
            words = [word for word in re.split(r'\s+', line) if word]
            if len(words) < 2 or len(words) > 4:
                continue
            if self._is_cv_section_heading(line):
                continue
            title_like = all(word[:1].isupper() for word in words if word[:1].isalpha())
            if title_like:
                return line
        return ''

    def _extract_skill_hints_from_cv(self, cv_text):
        raw_text = str(cv_text or '').lower()
        if not raw_text:
            return []

        normalized_text = re.sub(r'[^a-z0-9\s\+#\./-]', ' ', raw_text)
        normalized_text = re.sub(r'\s+', ' ', normalized_text)

        candidate_terms = set(self._SKILL_SYNONYMS.keys())
        candidate_terms.update(self._SKILL_SYNONYMS.values())
        candidate_terms.update(self._LANGUAGE_SKILL_NAMES)
        candidate_terms.update(self._LANGUAGE_SKILL_ALIASES.keys())

        found = set()
        for term in candidate_terms:
            normalized_term = self._normalize_skill_key(term)
            if not normalized_term:
                continue
            if re.search(r'[^a-z0-9\s]', normalized_term):
                if normalized_term in normalized_text:
                    found.add(self._canonical_skill_name(normalized_term) or normalized_term)
                continue
            pattern = r'\b%s\b' % re.escape(normalized_term)
            if re.search(pattern, normalized_text):
                found.add(self._canonical_skill_name(normalized_term) or normalized_term)

        ordered = sorted(skill for skill in found if skill)
        return ordered[:int(self._CV_EXTRACTION_MAX_HINT_SKILLS)]

    def _build_cv_extraction_hints(self, cv_text):
        return {
            'name_hint': self._extract_name_hint_from_cv(cv_text),
            'skill_candidates': self._extract_skill_hints_from_cv(cv_text),
        }

    def _compute_extraction_max_tokens(self, chunk_text):
        chunk_length = len(str(chunk_text or ''))
        dynamic_budget = int(self._CV_EXTRACTION_MIN_RESPONSE_TOKENS) + int(chunk_length / 12)
        return max(
            int(self._CV_EXTRACTION_MIN_RESPONSE_TOKENS),
            min(int(self._CV_EXTRACTION_MAX_RESPONSE_TOKENS), dynamic_budget),
        )

    def _merge_unique_text_list(self, base_values, incoming_values):
        merged_values = []
        seen = set()
        for source_values in (base_values or [], incoming_values or []):
            for raw_value in source_values:
                text = str(raw_value).strip()
                if not text:
                    continue
                fingerprint = text.lower()
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                merged_values.append(text)
        return merged_values

    def _pick_richer_text(self, current_value, new_value):
        current_text = str(current_value or '').strip()
        new_text = str(new_value or '').strip()
        if not current_text:
            return new_text
        if not new_text:
            return current_text
        return new_text if len(new_text) > len(current_text) else current_text

    def _merge_experience_skill_categories(self, base_skills, incoming_skills):
        merged_skills = self._normalize_experience_skills(base_skills)
        incoming_normalized = self._normalize_experience_skills(incoming_skills)
        for category_name in self._SKILL_PERTINENT_CATEGORIES:
            merged_skills[category_name] = self._merge_unique_text_list(
                merged_skills.get(category_name),
                incoming_normalized.get(category_name),
            )
        return merged_skills

    def _experience_fuzzy_key(self, experience):
        """Returns a normalized string key for fuzzy deduplication."""
        import unicodedata as _ud

        def _clean(value):
            text = str(value or '').lower().strip()
            text = ''.join(
                character for character in _ud.normalize('NFD', text)
                if _ud.category(character) != 'Mn'
            )
            text = re.sub(r'[^a-z0-9\s]', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text

        title_key = _clean(experience.get('title'))[:40]
        company_key = _clean(experience.get('company'))[:30]
        return (title_key, company_key)

    def _find_fuzzy_matching_experience(self, candidate_experience, experience_by_fingerprint):
        """
        Returns the matching existing experience dict if a close-enough match exists,
        otherwise returns None.
        Uses token overlap on (title, company) to handle LLM paraphrasing across chunks.
        """
        from difflib import SequenceMatcher

        candidate_title, candidate_company = self._experience_fuzzy_key(candidate_experience)

        for (existing_title, existing_company), existing_experience in experience_by_fingerprint.items():
            title_ratio = SequenceMatcher(None, candidate_title, existing_title).ratio()
            company_ratio = SequenceMatcher(None, candidate_company, existing_company).ratio()

            if title_ratio >= 0.82 and company_ratio >= 0.70:
                return existing_experience
            if title_ratio >= 0.95 and not existing_company and not candidate_company:
                return existing_experience

        return None

    def _merge_profiles(self, profiles):
        cleaned_profiles = [profile for profile in (profiles or []) if isinstance(profile, dict)]
        if not cleaned_profiles:
            return {
                'id': self.id,
                'name': self.partner_name or '',
                'education': {'degree': '', 'field': '', 'university': '', 'date': ''},
                'experiences': [],
                'experience_years': 0.0,
                'certification': [],
                'skills': {},
                'extraction_warnings': ['No profile data was extracted from the CV text.'],
            }

        merged = {
            'id': self.id,
            'name': '',
            'education': {'degree': '', 'field': '', 'university': '', 'date': ''},
            'experiences': [],
            'experience_years': 0.0,
            'certification': [],
            'skills': {},
        }

        experience_by_fingerprint = {}
        certification_seen = set()
        skill_scores = {}
        language_skill_ranks = {}

        for profile in cleaned_profiles:
            if not merged['name']:
                merged['name'] = str(profile.get('name') or '').strip()

            education = profile.get('education') if isinstance(profile.get('education'), dict) else {}
            for field_name in ('degree', 'field', 'university', 'date'):
                if not merged['education'][field_name]:
                    merged['education'][field_name] = str(education.get(field_name) or '').strip()

            for experience in (profile.get('experiences') or []):
                if not isinstance(experience, dict):
                    continue
                fuzzy_key = self._experience_fuzzy_key(experience)
                if not any(fuzzy_key):
                    continue

                existing_experience = self._find_fuzzy_matching_experience(experience, experience_by_fingerprint)

                if existing_experience is None:
                    normalized_experience = dict(experience)
                    normalized_experience['tasks'] = self._merge_unique_text_list(
                        normalized_experience.get('tasks'),
                        [],
                    )
                    normalized_experience['skills_pertinents'] = self._normalize_experience_skills(
                        normalized_experience.get('skills_pertinents')
                    )
                    normalized_experience = self._enrich_experience_sections_from_tasks(normalized_experience)
                    merged['experiences'].append(normalized_experience)
                    experience_by_fingerprint[fuzzy_key] = normalized_experience
                    continue

                for field_name in (
                    'general_context',
                    'project_topic',
                    'responsibilities',
                ):
                    existing_experience[field_name] = self._pick_richer_text(
                        existing_experience.get(field_name),
                        experience.get(field_name),
                    )

                for field_name in ('work_done', 'results_obtained'):
                    existing_experience[field_name] = self._merge_unique_text_list(
                        existing_experience.get(field_name),
                        self._normalize_narrative_list(experience.get(field_name)),
                    )

                existing_experience['tasks'] = self._merge_unique_text_list(
                    existing_experience.get('tasks'),
                    experience.get('tasks') if isinstance(experience.get('tasks'), list) else [],
                )
                existing_experience['skills_pertinents'] = self._merge_experience_skill_categories(
                    existing_experience.get('skills_pertinents'),
                    experience.get('skills_pertinents'),
                )
                existing_experience['duration'] = self._pick_richer_text(
                    existing_experience.get('duration'),
                    experience.get('duration'),
                )
                self._enrich_experience_sections_from_tasks(existing_experience)

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

                if self._is_language_skill(canonical_skill):
                    normalized_level = self._normalize_language_skill_level(raw_level)
                    if not normalized_level:
                        continue
                    level_rank = self._LANGUAGE_LEVELS.index(normalized_level) + 1
                    language_skill_ranks[canonical_skill] = max(
                        language_skill_ranks.get(canonical_skill, 0),
                        level_rank,
                    )
                    continue

                score = self._skill_level_to_score5(raw_level)
                if score <= 0:
                    continue
                skill_scores[canonical_skill] = max(skill_scores.get(canonical_skill, 0), score)

        merged_skills = {
            skill_name: self._GENERAL_LEVEL_BY_SCORE5.get(level, 'Beginner')
            for skill_name, level in skill_scores.items()
        }
        for skill_name, level_rank in language_skill_ranks.items():
            rank = max(1, min(len(self._LANGUAGE_LEVELS), int(level_rank)))
            merged_skills[skill_name] = self._LANGUAGE_LEVELS[rank - 1]

        merged['skills'] = merged_skills
        merged['experience_years'] = self._calculate_experience_years(merged['experiences'])
        merged['extraction_warnings'] = self._build_extraction_warnings(merged)
        return merged

    def _build_extraction_warnings(self, applicant_profile):
        warnings = []
        profile = applicant_profile if isinstance(applicant_profile, dict) else {}

        if not str(profile.get('name') or '').strip():
            warnings.append('Candidate name could not be extracted reliably.')

        education = profile.get('education') if isinstance(profile.get('education'), dict) else {}
        if not any(str(education.get(field_name) or '').strip() for field_name in ('degree', 'field', 'university', 'date')):
            warnings.append('Education details are incomplete or missing.')

        experiences = profile.get('experiences') if isinstance(profile.get('experiences'), list) else []
        if not experiences:
            warnings.append('No experience entries were extracted from the CV.')

        skills = profile.get('skills') if isinstance(profile.get('skills'), dict) else {}
        if not skills:
            warnings.append('No skills were extracted from the CV.')

        return warnings

    def _month_from_name(self, month_name):
        normalized = str(month_name or '').strip().lower().replace('.', '')
        normalized = ''.join(
            character
            for character in unicodedata.normalize('NFD', normalized)
            if unicodedata.category(character) != 'Mn'
        )
        return self._MONTH_NAME_TO_NUMBER.get(normalized, 0)

    def _duration_to_months_estimate(self, duration_text):
        normalized = self._normalize_duration_text(duration_text).lower()
        if not normalized:
            return 0

        years_match = re.search(r'(\d+)\s*(?:ans?|years?)\b', normalized)
        months_match = re.search(r'(\d+)\s*(?:mois|months?)\b', normalized)

        total_months = 0
        if years_match:
            total_months += int(years_match.group(1)) * 12
        if months_match:
            total_months += int(months_match.group(1))
        return total_months

    def _duration_to_year_interval(self, duration_text):
        normalized = self._normalize_duration_text(duration_text)
        if not normalized:
            return None

        lowered = normalized.lower()
        has_present = bool(re.search(r'\b(present|current|now|ongoing|actuel|actuelle|a ce jour|en cours)\b', lowered))

        # Pattern: MM/YYYY or MM-YYYY
        numeric_month_year_matches = list(
            re.finditer(r'\b(0?[1-9]|1[0-2])[\-/]((?:19|20)\d{2})\b', lowered)
        )
        numeric_month_year_points = [
            (int(match.group(2)), int(match.group(1)))
            for match in numeric_month_year_matches
        ]

        if numeric_month_year_points:
            start_year, start_month = numeric_month_year_points[0]
            if has_present:
                today = date.today()
                end_year, end_month = today.year, today.month
            else:
                end_year, end_month = numeric_month_year_points[-1]

            if (end_year, end_month) < (start_year, start_month):
                start_year, start_month, end_year, end_month = end_year, end_month, start_year, start_month
            return (start_year, start_month, end_year, end_month)

        # Pattern: YYYY-MM or YYYY/MM
        year_month_matches = list(
            re.finditer(r'\b((?:19|20)\d{2})[\-/](0?[1-9]|1[0-2])\b', lowered)
        )
        year_month_points = [
            (int(match.group(1)), int(match.group(2)))
            for match in year_month_matches
        ]

        if year_month_points:
            start_year, start_month = year_month_points[0]
            if has_present:
                today = date.today()
                end_year, end_month = today.year, today.month
            else:
                end_year, end_month = year_month_points[-1]

            if (end_year, end_month) < (start_year, start_month):
                start_year, start_month, end_year, end_month = end_year, end_month, start_year, start_month
            return (start_year, start_month, end_year, end_month)

        month_year_matches = list(
            re.finditer(
                r'\b('
                r'jan(?:uary)?|janv(?:ier)?|'
                r'feb(?:ruary)?|fev(?:r(?:ier)?)?|'
                r'mar(?:ch)?|mars|'
                r'apr(?:il)?|avr(?:il)?|'
                r'may|mai|'
                r'jun(?:e)?|juin|'
                r'jul(?:y)?|juil(?:let)?|'
                r'aug(?:ust)?|aout|'
                r'sep(?:t|tember)?|septembre|'
                r'oct(?:ober)?|octobre|'
                r'nov(?:ember)?|novembre|'
                r'dec(?:ember)?|decembre'
                r')\s+((?:19|20)\d{2})\b',
                lowered,
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
            if has_present:
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

        if has_present:
            today = date.today()
            end_year = today.year
            end_month = today.month

        if (end_year, end_month) < (start_year, start_month):
            start_year, start_month, end_year, end_month = end_year, end_month, start_year, start_month

        return (start_year, start_month, end_year, end_month)

    def _calculate_experience_years(self, experiences):
        intervals = []
        fallback_months = 0
        for experience in experiences:
            if not isinstance(experience, dict):
                continue
            interval = self._duration_to_year_interval(experience.get('duration'))
            if interval:
                intervals.append(interval)
                continue
            fallback_months += self._duration_to_months_estimate(experience.get('duration'))

        if not intervals:
            return round(float(fallback_months) / 12.0, 1)

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

        total_months = 0
        for start_year, start_month, end_year, end_month in merged_intervals:
            start_index = (start_year * 12) + start_month
            end_index = (end_year * 12) + end_month
            months_span = max(1, end_index - start_index + 1)
            total_months += months_span

        if fallback_months > 0:
            safe_fallback = min(fallback_months, max(1, int(total_months * 0.20)))
            total_months += safe_fallback

        return round(float(total_months) / 12.0, 1)

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

    def _normalize_skill_category_key(self, value):
        normalized = str(value or '').strip().lower()
        normalized = ''.join(
            character
            for character in unicodedata.normalize('NFD', normalized)
            if unicodedata.category(character) != 'Mn'
        )
        normalized = re.sub(r'[^a-z0-9\s]', ' ', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return normalized

    def _empty_skill_categories(self):
        return {
            category: []
            for category in self._SKILL_PERTINENT_CATEGORIES
        }

    def _normalize_skill_category_name(self, raw_category):
        category_key = self._normalize_skill_category_key(raw_category)
        if not category_key:
            return ''
        if category_key in self._SKILL_CATEGORY_ALIASES:
            return self._SKILL_CATEGORY_ALIASES[category_key]

        for category in self._SKILL_PERTINENT_CATEGORIES:
            if self._normalize_skill_category_key(category) == category_key:
                return category
        return ''

    def _guess_skill_category(self, skill_name):
        skill_key = self._normalize_skill_key(skill_name)
        if not skill_key:
            return 'Technologies'

        if re.search(r'\b(communication|leadership|teamwork|collaboration|adaptability|creativity|problem solving|negotiation|autonomy|empathy|time management)\b', skill_key):
            return 'Soft Skills'
        if re.search(r'\b(excel|power bi|tableau|sap|salesforce|figma|photoshop|illustrator|autocad|solidworks|jira|confluence|odoo|ms project|notion|wordpress)\b', skill_key):
            return 'Logiciels'
        if re.search(r'\b(python|java|javascript|typescript|php|ruby|go|golang|rust|kotlin|swift|scala|perl|r\b|matlab|sql|plsql|bash|powershell|c\+\+|c#|c\b|objective c|dart|vba)\b', skill_key):
            return 'Langages de programmation'
        if re.search(r'\b(arduino|raspberry|plc|automate|microcontroller|fpga|sensor|capteur|oscilloscope|router|switch|server rack|printer|scanner)\b', skill_key):
            return 'Matériels'
        if re.search(r'\b(agile|scrum|kanban|lean|six sigma|itil|waterfall|safe|design thinking|prince2)\b', skill_key):
            return 'Méthodes'
        if re.search(r'\b(iso\s*\d+|iso|rgpd|gdpr|tcp/ip|http|https|mqtt|opc ua|oauth|tls|ssl|pci dss|rest|soap|hl7)\b', skill_key):
            return 'Normes et protocoles'
        if re.search(r'\b(linux|windows|macos|unix|ubuntu|debian|red hat|android|ios|vmware|citrix)\b', skill_key):
            return 'Systèmes'
        if re.search(r'\b(seo|sem|google ads|meta ads|content marketing|email marketing|marketing automation|crm campaign|branding|growth hacking)\b', skill_key):
            return 'Marketing'
        return 'Technologies'

    def _normalize_experience_skills(self, raw_skills):
        categorized_skills = self._empty_skill_categories()
        seen_skills_by_category = {
            category: set()
            for category in categorized_skills
        }

        if isinstance(raw_skills, dict):
            source_items = raw_skills.items()
        elif isinstance(raw_skills, list):
            source_items = [('Technologies', raw_skills)]
        else:
            source_items = []

        for raw_category, raw_values in source_items:
            category_name = self._normalize_skill_category_name(raw_category)

            if isinstance(raw_values, list):
                values = raw_values
            elif isinstance(raw_values, str):
                values = [raw_values]
            else:
                continue

            for raw_value in values:
                skill_name = str(raw_value).strip()
                if not skill_name:
                    continue

                target_category = category_name or self._guess_skill_category(skill_name)
                if target_category not in categorized_skills:
                    target_category = 'Technologies'

                fingerprint = skill_name.lower()
                if fingerprint in seen_skills_by_category[target_category]:
                    continue

                seen_skills_by_category[target_category].add(fingerprint)
                categorized_skills[target_category].append(skill_name)

        return categorized_skills

    def _iter_experience_skill_names(self, raw_skills):
        if isinstance(raw_skills, dict):
            for category_name in self._SKILL_PERTINENT_CATEGORIES:
                for skill_name in (raw_skills.get(category_name) or []):
                    cleaned = str(skill_name).strip()
                    if cleaned:
                        yield cleaned
            return

        if isinstance(raw_skills, list):
            for skill_name in raw_skills:
                cleaned = str(skill_name).strip()
                if cleaned:
                    yield cleaned

    def _normalize_task_sentences(self, tasks):
        normalized_tasks = []
        seen = set()
        for raw_task in (tasks or []):
            task_text = str(raw_task or '').strip()
            if not task_text:
                continue
            task_text = re.sub(r'\s+', ' ', task_text)
            fingerprint = task_text.lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            normalized_tasks.append(task_text)
        return normalized_tasks

    def _normalize_narrative_list(self, values):
        if isinstance(values, str):
            text_value = values.strip()
            return [text_value] if text_value else []
        if not isinstance(values, list):
            return []

        normalized_values = []
        seen = set()
        for raw_value in values:
            text_value = str(raw_value or '').strip()
            if not text_value:
                continue
            fingerprint = text_value.lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            normalized_values.append(text_value)
        return normalized_values

    def _build_task_based_narrative_sections(self, tasks):
        normalized_tasks = self._normalize_task_sentences(tasks)
        if not normalized_tasks:
            return {}

        tasks_block = '; '.join(normalized_tasks)
        return {
            'general_context': (
                'Le contexte general de la mission est constitue des activites suivantes executees sur le perimetre '
                'du poste: %s.'
            ) % tasks_block,
            'project_topic': (
                'Le sujet principal du projet, tel qu il ressort des actions decrites dans le CV, porte sur les '
                'travaux suivants: %s.'
            ) % tasks_block,
            'responsibilities': (
                'Les responsabilites occupees couvrent la prise en charge des volets suivants, avec une implication '
                'operationnelle continue: %s.'
            ) % tasks_block,
            'work_done': (
                [
                    (
                        'Le travail realise est detaille par l ensemble des taches suivantes, toutes effectivement '
                        'mentionnees dans l experience: %s.'
                    ) % tasks_block
                ]
            ),
            'results_obtained': (
                [
                    (
                        'Les resultats obtenus, observables a partir des taches explicitement decrites dans le CV, '
                        'se materialisent par la realisation concrete des actions suivantes: %s.'
                    ) % tasks_block
                ]
            ),
        }

    def _enrich_experience_sections_from_tasks(self, experience):
        if not isinstance(experience, dict):
            return experience

        normalized_tasks = self._normalize_task_sentences(experience.get('tasks'))
        experience['tasks'] = normalized_tasks
        experience['work_done'] = self._normalize_narrative_list(experience.get('work_done'))
        experience['results_obtained'] = self._normalize_narrative_list(experience.get('results_obtained'))
        if not normalized_tasks:
            return experience

        task_based_sections = self._build_task_based_narrative_sections(normalized_tasks)
        for field_name in (
            'general_context',
            'project_topic',
            'responsibilities',
            'work_done',
            'results_obtained',
        ):
            if field_name in ('work_done', 'results_obtained'):
                current_values = self._normalize_narrative_list(experience.get(field_name))
                if not current_values:
                    experience[field_name] = self._normalize_narrative_list(task_based_sections.get(field_name))
                else:
                    experience[field_name] = current_values
                continue

            current_value = str(experience.get(field_name) or '').strip()
            if len(current_value) < 40:
                experience[field_name] = task_based_sections.get(field_name, current_value)
        return experience

    def _validate_ai_profile_structure(self, payload):
        """
        Validates the raw LLM JSON payload structure before normalization.
        Returns a list of warning strings describing structural issues found.
        These are non-fatal: normalization continues, but issues are logged.
        """
        warnings = []
        if not isinstance(payload, dict):
            return ['LLM response root is not a JSON object.']

        for required_key in ('name', 'education', 'experiences', 'skills'):
            if required_key not in payload:
                warnings.append('Missing top-level key: "%s".' % required_key)

        education = payload.get('education')
        if education is not None and not isinstance(education, dict):
            warnings.append('"education" field is not an object (got %s).' % type(education).__name__)

        experiences = payload.get('experiences')
        if experiences is not None and not isinstance(experiences, list):
            warnings.append('"experiences" field is not a list (got %s).' % type(experiences).__name__)
        elif isinstance(experiences, list):
            for exp_index, exp in enumerate(experiences):
                if not isinstance(exp, dict):
                    warnings.append('Experience[%d] is not an object.' % exp_index)
                    continue
                for exp_key in ('title', 'company', 'duration'):
                    if not str(exp.get(exp_key) or '').strip():
                        warnings.append('Experience[%d] has empty "%s".' % (exp_index, exp_key))
                for list_key in ('tasks', 'work_done', 'results_obtained'):
                    value = exp.get(list_key)
                    if value is not None and not isinstance(value, list):
                        warnings.append(
                            'Experience[%d].%s should be a list (got %s).'
                            % (exp_index, list_key, type(value).__name__)
                        )

        skills = payload.get('skills')
        if skills is not None and not isinstance(skills, dict):
            warnings.append('"skills" field is not an object (got %s).' % type(skills).__name__)

        return warnings

    def _normalize_ai_profile(self, payload):
        if not isinstance(payload, dict):
            payload = {}

        structure_warnings = self._validate_ai_profile_structure(payload)
        if structure_warnings:
            for warning in structure_warnings:
                _logger.warning('AI profile structure issue: %s', warning)

        education = payload.get('education') if isinstance(payload.get('education'), dict) else {}
        experiences = payload.get('experiences') if isinstance(payload.get('experiences'), list) else []
        skills = payload.get('skills') if isinstance(payload.get('skills'), dict) else {}

        normalized_experiences = []
        for experience in experiences:
            if not isinstance(experience, dict):
                continue
            tasks = experience.get('tasks') if isinstance(experience.get('tasks'), list) else []
            skills_pertinents = self._normalize_experience_skills(experience.get('skills_pertinents'))
            normalized_experience = {
                'title': experience.get('title') or '',
                'company': experience.get('company') or '',
                'duration': self._normalize_duration_text(experience.get('duration')),
                'general_context': str(experience.get('general_context') or '').strip(),
                'project_topic': str(experience.get('project_topic') or '').strip(),
                'responsibilities': str(experience.get('responsibilities') or '').strip(),
                'work_done': self._normalize_narrative_list(experience.get('work_done')),
                'results_obtained': self._normalize_narrative_list(experience.get('results_obtained')),
                'tasks': [str(task).strip() for task in tasks if str(task).strip()],
                'skills_pertinents': skills_pertinents,
            }
            normalized_experiences.append(self._enrich_experience_sections_from_tasks(normalized_experience))

        section_general_skills = {}
        section_language_skills = {}
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
                if normalized_level:
                    section_language_skills[canonical_name] = normalized_level
            else:
                normalized_level = self._normalize_general_skill_level(skill_level)
                if normalized_level:
                    section_general_skills[canonical_name] = normalized_level

        # Keep skills focused on: experience-relevant skills, extracted languages,
        # and only strong additional skills explicitly present in the CV skills section.
        relevant_skill_names = set()
        for experience in normalized_experiences:
            for skill_name in self._iter_experience_skill_names(experience.get('skills_pertinents')):
                canonical_name = self._canonical_skill_name(skill_name)
                if canonical_name:
                    relevant_skill_names.add(canonical_name)

        normalized_skills = {}

        for skill_name, level in section_language_skills.items():
            normalized_skills[skill_name] = level

        for skill_name in relevant_skill_names:
            if skill_name in section_language_skills:
                normalized_skills[skill_name] = section_language_skills[skill_name]
            elif skill_name in section_general_skills:
                normalized_skills[skill_name] = section_general_skills[skill_name]
            elif not self._is_language_skill(skill_name):
                normalized_skills[skill_name] = 'Intermediate'

        for skill_name, level in section_general_skills.items():
            if self._skill_level_to_score5(level) >= int(self._GOOD_SKILL_MIN_SCORE5):
                normalized_skills.setdefault(skill_name, level)

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
                'date': education.get('date') or '',
            },
            'experiences': normalized_experiences,
            'experience_years': experience_years,
            'certification': normalized_certifications,
            'skills': normalized_skills,
            'extraction_warnings': [],
        }

    def _extract_profile_with_groq(self, cv_text):
        self.ensure_one()
        import time as _time

        normalized_cv_text = self._normalize_cv_text_for_extraction(cv_text)
        extraction_hints = self._build_cv_extraction_hints(normalized_cv_text)

        system_prompt = (
            'You are an expert CV parser. '\
            'Return ONLY valid JSON with this exact top-level structure: '\
            '{"id": int, "name": str, "education": {"degree": str, "field": str, "university": str, "date": str}, '\
            '"experiences": [{"title": str, "company": str, "duration": str, "general_context": str, "project_topic": str, "responsibilities": str, "work_done": [str], "results_obtained": [str], "tasks": [str], "skills_pertinents": {'\
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
            'Language rule: write all extracted textual content strictly in French, including tasks and all narrative fields. '\
            'If source text is in another language, translate faithfully into natural professional French without losing meaning. '\
            'Do not infer results from responsibilities. '\
            'If one of these 5 narrative fields is missing but tasks are present, build it from task sentences instead of leaving it empty. '\
            'Narrative richness rules: for each experience and each of the 5 narrative fields, when evidence exists, write 2 to 4 complete sentences and at least 30 words. '\
            'Each field must be specific and non-generic, grounded in concrete facts from the same experience. '\
            'general_context must mention business/domain context and mission scope. '\
            'project_topic must mention project objective and functional focus. '\
            'responsibilities must describe ownership, decisions, and accountability perimeter. '\
            'work_done must detail concrete actions, tools/methods, and execution scope. '\
            'results_obtained must describe factual outcomes and quantified impact when present in CV text. '\
            'Avoid short vague fillers like "participation a" without details. '\
            'Only keep a narrative field empty when no evidence and no tasks exist for that experience. '\
            'Tasks rule: extract all distinct task bullets/sentences for each experience when present; do not summarize tasks. '\
            'Keep task items concise and deduplicated. '\
            'In "skills", prioritize languages and skills explicitly listed in the CV skills section. '\
            'For technical and soft skills, use exactly one of: Beginner, Elementary, Intermediate, Advanced, Expert. '\
            'For language skills, use exactly one of: A1, A2, B1, B2, C1, C2. '\
            'Always include spoken languages inside "skills" with CEFR levels when available. '\
            'Use canonical language names when possible: francais, anglais, arabe, espagnol, allemand, italien, portugais. '\
            'Do not include markdown, comments, or explanations.'
        )

        chunks = self._split_cv_text_chunks(normalized_cv_text)
        if not chunks:
            return self._normalize_ai_profile({})

        profiles = []
        chunk_errors = []
        for chunk_index, chunk in enumerate(chunks, start=1):
            user_prompt = (
                'Extract CV data from the text below. '\
                'Set "id" to %s. '\
                'If a value is missing, use empty string, empty list, or empty object. '\
                'A deterministic pre-parser generated these optional hints: %s. '\
                'Use them only when they are explicitly supported by the current CV chunk text. '\
                'Identify experiences first, then extract details. '\
                'Write all text outputs in French only (no English): title, company, tasks, and all narrative fields must be in French. '\
                'If the CV uses another language, translate extracted content to French while preserving factual meaning. '\
                'For each experience, always include: general_context, project_topic, responsibilities, work_done, results_obtained. '\
                'Return work_done and results_obtained as arrays of strings (not a single string). '\
                'Extract all distinct tasks mentioned for that same experience (bullets or action sentences), do not compress them into one summary line. '\
                'Build the 5 narrative fields from tasks when direct text is sparse, and keep them long, detailed, and fully grounded in the extracted tasks. '\
                'Use all relevant task sentences for that experience when composing those fields. '\
                'Mandatory quality gate before final JSON: when evidence exists, each narrative field must have at least 30 words and 2 complete sentences. '\
                'If a field is too short, expand it with missing context from tasks of the same experience before returning JSON. '\
                'Prefer precise nouns and action verbs from the CV over generic wording. '\
                'Only keep a narrative field empty if there is no supporting evidence and no tasks. '\
                'Always include skills_pertinents as a dictionary of categories for each experience. '\
                'In top-level skills, include languages and explicit skills-section items first. '\
                'Use string proficiency levels (not numeric). '\
                'Chunk %s/%s of a longer CV.\n\nCV TEXT:\n%s'
            ) % (
                self.id,
                json.dumps(extraction_hints, ensure_ascii=False),
                chunk_index,
                len(chunks),
                chunk,
            )

            last_error = None
            ai_payload = None

            for attempt in range(1, 4):
                try:
                    ai_payload = self._call_groq_json(
                        system_prompt,
                        user_prompt,
                        max_tokens=self._compute_extraction_max_tokens(chunk),
                        stage='extraction',
                    )
                    break
                except Exception as chunk_error:
                    last_error = chunk_error
                    _logger.warning(
                        'Chunk %s/%s extraction attempt %s failed: %s',
                        chunk_index, len(chunks), attempt, chunk_error,
                    )
                    if attempt < 3:
                        _time.sleep(2 ** (attempt - 1))

            if ai_payload is not None:
                profiles.append(self._normalize_ai_profile(ai_payload))
            else:
                error_msg = 'Chunk %s/%s failed after 3 attempts: %s' % (chunk_index, len(chunks), last_error)
                _logger.error(error_msg)
                chunk_errors.append(error_msg)
                profiles.append(self._normalize_ai_profile({}))

        merged = self._merge_profiles(profiles)
        if not str(merged.get('name') or '').strip() and extraction_hints.get('name_hint'):
            merged['name'] = extraction_hints['name_hint']

        merged['chunk_count'] = len(chunks)
        if chunk_errors:
            existing_warnings = list(merged.get('extraction_warnings') or [])
            merged['extraction_warnings'] = existing_warnings + chunk_errors

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
            'Tu es un ATS IA et recruteur technique senior. '
            'Evalue strictement un candidat par rapport a un poste en te basant uniquement sur les donnees JSON fournies.\n\n'
            'Règles globales:\n'
            '1) Zero hallucination: aucune information inventee, aucune hypothese externe.\n'
            '2) Autorite du job: l evaluation doit etre strictement relative a job.\n'
            '3) Langue de sortie: tout le contenu textuel final doit etre en francais.\n'
            '4) Si job.education est vide -> education=15. Si exigences de langues absentes -> langues=10.\n'
            '5) Penalite recence: competence coeur non utilisee depuis >24 mois -> -15% sur sa contribution.\n'
            '6) Densite experience: si poste Senior/Lead/Architect/Manager et titres candidat uniquement Junior/Intern/Trainee -> -10 points sur experience.\n'
            '7) Stabilite: tenure moyenne <12 mois -> ajouter risque de job hopping + question d entretien specifique.\n'
            '8) Proximite d outil: outil exact absent mais concurrent direct maitrise -> 50% du credit, et mentionner "Equivalent tool mastered - verification required" dans ambiguities_to_verify.\n'
            '9) Alignement education: domaine fortement non aligne (ex: Biologie pour Data Science) -> education=0, sauf compensation partielle par certifications specialisees pertinentes.\n\n'
            '10) Interdiction d evaluation generale: ne jamais commenter le candidat en dehors des exigences du poste (pas de jugement global de personnalite, potentiel, ou valeur generale).\n'
            '11) Traçabilite obligatoire: chaque item de strengths/risks/missing_requirements/bonus_matches doit etre rattache explicitement a un critere du poste (competence, responsabilite, niveau, domaine, langue, outillage, anciennete).\n'
            '12) Neutralite hors-perimetre: les informations du candidat non demandees par job doivent etre ignorees, sans bonus ni malus.\n'
            '13) Priorite aux gaps critiques: un manque critique requis par job doit peser plus qu un bonus non requis.\n\n'
            'Bareme total 100:\n'
            '- competences_techniques: 0..40\n'
            '- experience: 0..35\n'
            '- education: 0..15\n'
            '- langues: 0..10\n\n'
            'Fit level:\n'
            '- Adequation forte: score >= 75 et pas de manque critique bloquant\n'
            '- Adequation moderee: score 50..74 ou incertitudes importantes\n'
            '- Adequation faible: score < 50 ou manque critique bloquant\n\n'
            'Sortie: retourne uniquement un JSON valide et rien d autre, avec cette structure exacte:\n'
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
            '    "interview_questions": ["string (Questions techniques dures basées sur les risques)"],\n'
            '    "recommendation": "Poursuivre|Poursuivre avec prudence|Rejeter"\n'
            '  }\n'
            '}'
        )

        prompt_payload = {
            'candidate': applicant_data,
            'job': normalized_job_data,
        }

        user_prompt = (
            'Analyse le candidat contre ce poste en appliquant strictement les regles du system_prompt.\n'
            'Priorites d execution:\n'
            '1) comparer skills, experience, education et langues;\n'
            '2) appliquer les ajustements (recence, seniorite, stabilite, equivalence outils, alignement education);\n'
            '3) calculer les 4 sous-scores dans leurs bornes;\n'
            '4) deduire fit_level et recommendation;\n'
            '5) produire des risques et questions d entretien actionnables.\n'
            '6) chaque phrase de feedback doit citer un lien explicite au poste (requis/souhaite/non requis).\n'
            '7) ignorer totalement les elements du candidat hors perimetre du poste.\n'
            '8) ne pas produire de conclusion generale: uniquement une conclusion d adequation au poste cible.\n\n'
            'Format attendu par item (obligatoire quand applicable): "[Critere job] -> [Evidence candidat] -> [Impact sur score]".\n\n'
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
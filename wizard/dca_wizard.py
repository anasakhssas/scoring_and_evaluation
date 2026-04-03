from odoo import fields, models
import json

# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


class DcaWizard(models.TransientModel) :

    _name = 'dca.wizard'

    report_models = fields.Selection([('alten', 'Alten'), ('simplified', 'Simplify')], 'model de dossier',  required=True, default='simplified')
    applicant_id = fields.Many2one('hr.applicant', 'candidate')
    code_job = fields.Char('Reference de poste')

    def get_applicant_extracted_payload(self) :
        raw_payload = self.applicant_id.applicant_extracted_json or '{}'
        if isinstance(raw_payload, dict) :
            return raw_payload
        
        try:
            payload = json.loads(raw_payload)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}
    
    def print_report(self) :
        self.ensure_one()
        applicant_data = self.get_applicant_extracted_payload()
        code_job_value = (self.code_job or '').strip()

        if not applicant_data :
            applicant_data = self.applicant_id.get_extracted_applicant_data()
        
        if self.report_models == 'alten' : 
            return self.env.ref('scoring_candidates.action_print_dca_alten').with_context(
                dca_code_job=code_job_value
            ).report_action(self.applicant_id)
        elif self.report_models == 'simplified' :
            return self.env.ref('scoring_candidates.action_print_dca_simplify').with_context(
                dca_code_job=code_job_value
            ).report_action(self.applicant_id)
        
        return True
import json

from odoo import models


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'

    def get_extracted_job_data(self):
        self.ensure_one()
        return self.env['extract.job.info'].get_job_data(applicant=self)

    def action_show_extracted_job_data(self):
        self.ensure_one()
        extracted_data = self.get_extracted_job_data()
        wizard = self.env['applicant.job.extraction.wizard'].create({
            'applicant_id': self.id,
            'extracted_data': json.dumps(extracted_data, indent=4),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': 'Extracted Job Data',
            'res_model': 'applicant.job.extraction.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }

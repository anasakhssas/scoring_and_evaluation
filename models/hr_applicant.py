from odoo import models


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'

    def get_extracted_job_data(self):
        self.ensure_one()
        return self.env['extract.job.info'].get_job_data(applicant=self)

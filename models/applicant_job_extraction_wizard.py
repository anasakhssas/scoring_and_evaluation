from odoo import fields, models


class ApplicantJobExtractionWizard(models.TransientModel):
    _name = 'applicant.job.extraction.wizard'
    _description = 'Applicant Job Extraction Preview'

    applicant_id = fields.Many2one('hr.applicant', string='Applicant', readonly=True)
    extracted_data = fields.Text(string='Extracted Data', readonly=True)

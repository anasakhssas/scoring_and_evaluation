from odoo import fields, models


class ApplicantExtractionPreviewWizard(models.TransientModel):
    _name = 'applicant.extraction.preview.wizard'
    _description = 'Applicant Extraction Preview Wizard'

    applicant_id = fields.Many2one('hr.applicant', string='Applicant', readonly=True)
    preview_title = fields.Char(string='Preview', readonly=True)
    extracted_data = fields.Text(string='Extracted Data', readonly=True)

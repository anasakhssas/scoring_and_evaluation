from odoo import fields, models


class HrJob(models.Model):
    _inherit = 'hr.job'

    major = fields.Char(
        string='Major',
        help='Primary field of study expected for this job (e.g., Computer Science, Finance).',
    )

    min_exp_years = fields.Float(
        string='Minimum Experience (Years)',
        default=0.0,
        help='Minimum required experience for the job, in years.',
    )
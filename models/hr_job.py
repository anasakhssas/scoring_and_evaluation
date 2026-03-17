from odoo import fields, models


class HrJob(models.Model):
    _inherit = 'hr.job'

    min_exp_years = fields.Float(
        string='Minimum Experience (Years)',
        default=0.0,
        help='Minimum required experience for the job, in years.',
    )
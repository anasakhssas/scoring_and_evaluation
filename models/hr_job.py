from odoo import fields, models


class HrJobMajor(models.Model):
    _name = 'hr.job.major'
    _description = 'Job Major'
    _order = 'name'

    name = fields.Char(required=True)


class HrJob(models.Model):
    _inherit = 'hr.job'

    major_id = fields.Many2one(
        comodel_name='hr.job.major',
        string='Major',
        ondelete='restrict',
        help='Primary field of study expected for this job (e.g., Computer Science, Finance).',
    )

    major = fields.Char(
        related='major_id.name',
        string='Major',
        store=True,
        readonly=True,
    )

    min_exp_years = fields.Float(
        string='Minimum Experience (Years)',
        default=0.0,
        help='Minimum required experience for the job, in years.',
    )
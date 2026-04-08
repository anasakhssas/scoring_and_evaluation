from odoo import api, models


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model_create_multi
    def create(self, vals_list):
        attachments = super().create(vals_list)
        attachments._trigger_auto_scoring_for_applicants()
        return attachments

    def write(self, vals):
        result = super().write(vals)
        trigger_fields = {'res_model', 'res_id', 'mimetype', 'name', 'index_content', 'datas'}
        if trigger_fields.intersection(vals.keys()):
            self._trigger_auto_scoring_for_applicants()
        return result

    def _trigger_auto_scoring_for_applicants(self):
        applicant_model = self.env['hr.applicant']
        applicants = applicant_model.browse()

        for attachment in self:
            if attachment.res_model != 'hr.applicant' or not attachment.res_id:
                continue

            mimetype = (attachment.mimetype or '').lower()
            filename = (attachment.name or '').lower()
            if mimetype != 'application/pdf' and not filename.endswith('.pdf'):
                continue

            applicant = applicant_model.browse(attachment.res_id).exists()
            if applicant:
                applicants |= applicant

        if applicants:
            applicants.sudo()._auto_run_scoring_if_ready()
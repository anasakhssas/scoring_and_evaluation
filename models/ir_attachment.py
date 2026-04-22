import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    def _safe_refresh_hidden_snapshots(self, applicants):
        """Refresh applicant snapshot hook only when available.

        This avoids breaking uploads if another module did not provide the
        optional ``_refresh_hidden_snapshots`` method on ``hr.applicant``.
        """
        if not applicants:
            return

        if not hasattr(applicants, '_refresh_hidden_snapshots'):
            return

        try:
            applicants._refresh_hidden_snapshots()
        except Exception:
            # Never block binary upload flows because of a non-critical refresh hook.
            _logger.exception('Failed to refresh hidden applicant snapshots for ids %s', applicants.ids)

    def _refresh_hr_applicant_snapshots(self):
        applicants = self.filtered(
            lambda attachment: attachment.res_model == 'hr.applicant' and bool(attachment.res_id)
        )
        if not applicants:
            return

        applicant_ids = applicants.mapped('res_id')
        applicant_records = self.env['hr.applicant'].sudo().browse(applicant_ids).exists()
        self._safe_refresh_hidden_snapshots(applicant_records)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._refresh_hr_applicant_snapshots()
        return records

    def write(self, vals):
        before = self.filtered(
            lambda attachment: attachment.res_model == 'hr.applicant' and bool(attachment.res_id)
        ).mapped('res_id')
        result = super().write(vals)
        after = self.filtered(
            lambda attachment: attachment.res_model == 'hr.applicant' and bool(attachment.res_id)
        ).mapped('res_id')
        applicant_ids = list(set(before + after))
        if applicant_ids:
            applicant_records = self.env['hr.applicant'].sudo().browse(applicant_ids).exists()
            self._safe_refresh_hidden_snapshots(applicant_records)
        return result

    def unlink(self):
        applicant_ids = self.filtered(
            lambda attachment: attachment.res_model == 'hr.applicant' and bool(attachment.res_id)
        ).mapped('res_id')
        result = super().unlink()
        if applicant_ids:
            applicant_records = self.env['hr.applicant'].sudo().browse(applicant_ids).exists()
            self._safe_refresh_hidden_snapshots(applicant_records)
        return result
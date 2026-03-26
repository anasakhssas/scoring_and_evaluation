from odoo import models

class JobExtractor(models.AbstractModel):
    _name = 'extract.job.info'
    _description = 'Job information extractor for applicant scoring'

    def _extract_skills_to_dict(self, skill_records) :
        skills_dict = {}
        for line in skill_records :
            name = line.skill_id.name
            level = line.skill_level_id.name
            if name :
                skills_dict[name] = level
        
        return skills_dict

    def _prepare_job_payload(self, job):
        return {
            'job_id': job.id,
            'title': job.name,
            'education': job.expected_degree.name,
            'major': job.major_id.name or '',
            'min_exp_years': job.min_exp_years or 0.0,
            'skills': self._extract_skills_to_dict(job.current_job_skill_ids),
        }

    def get_job_data(self, applicant=None) :
        # If an applicant is provided, extract only the linked job information.
        if applicant:
            if not applicant.job_id:
                return []
            return [self._prepare_job_payload(applicant.job_id)]
    
    

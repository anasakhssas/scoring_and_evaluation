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
            'min_exp_years': None,
            'skills': self._extract_skills_to_dict(job.current_job_skill_ids),
        }

    def get_job_data(self, applicant=None) :
        # If an applicant is provided, extract only the linked job information.
        if applicant:
            if not applicant.job_id:
                return []
            return [self._prepare_job_payload(applicant.job_id)]

        # Otherwise, return all available jobs.
        jobs = self.env['hr.job'].search([])
        job_list = []
        for job in jobs :
            job_list.append(self._prepare_job_payload(job))

        return job_list

    def get_job_data_from_applicant_id(self, applicant_id):
        applicant = self.env['hr.applicant'].browse(applicant_id).exists()
        if not applicant:
            return []
        return self.get_job_data(applicant=applicant)
    
    

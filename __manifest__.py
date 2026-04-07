{
    'name': 'Job Information Extractor',
    'version': '1.0',
    'category': 'Human Resources',
    'summary': 'Extracts job details for LLM matching',
    'author': 'ACHMITECH',
    'license': 'LGPL-3',
    'depends': ['hr_recruitment'],  # Crucial: This ensures hr.job exists
    'external_dependencies': {
        'python': ['docx'],
    },
    'data': [
        'security/ir.model.access.csv', # You need to give yourself permission to run the code
        'data/hr_job_major_data.xml',
        'views/hr_applicant_views.xml',
        'views/hr_job_views.xml',
        'wizard/dca_wizard.xml',
        'report/dca_simplify.xml',
        'report/dca_alten.xml'
    ],
    'installable': True,
    'application': False,
}
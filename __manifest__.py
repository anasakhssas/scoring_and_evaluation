{
    'name': 'Job Information Extractor',
    'version': '1.0',
    'category': 'Human Resources',
    'summary': 'Extracts job details for LLM matching',
    'depends': ['hr_recruitment'],  # Crucial: This ensures hr.job exists
    'data': [
        'security/ir.model.access.csv', # You need to give yourself permission to run the code
    ],
    'installable': True,
    'application': False,
}
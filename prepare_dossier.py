from docx import Document
import re

def main():
    doc = Document("./data/dossier de competences.docx")
    for paragraph in doc.paragraphs:
        if "Nom Prénom" in paragraph.text:
            paragraph.text = paragraph.text.replace("Nom Prénom", "{{ name }}")
        if "Dernier Diplôme" in paragraph.text:
            paragraph.text = paragraph.text.replace("Dernier Diplôme", "{{ degree }}")
        if "Années d'Expériences" in paragraph.text:
            paragraph.text = paragraph.text.replace("Années d'Expériences", "{{ exp_years }}")
        
        # We will stop here. Modifying docx is tricky with loops.

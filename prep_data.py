import os
import pandas as pd

# Create the eval directory if it doesn't exist
os.makedirs("eval", exist_ok=True)

print("Loading Kaggle Resume.csv...")
try:
    df = pd.read_csv("Resume.csv")
except FileNotFoundError:
    print("Error: Could not find Resume.csv. Please place it in the same folder as this script.")
    exit(1)

# Pick 5 standard categories from the Kaggle dataset
target_categories = ['HR', 'ENGINEERING', 'INFORMATION-TECHNOLOGY', 'SALES', 'ADVOCATE']

# 1. Format 50 Resumes (10 from each category)
print("Extracting and formatting 50 resumes...")
resumes = df[df['Category'].isin(target_categories)].groupby('Category').head(10).reset_index()

resumes_clean = pd.DataFrame({
    'id': [f"res_{i}" for i in range(50)],
    'text': resumes['Resume_str'],
    'category': resumes['Category']
})
resumes_clean.to_csv("eval/resumes.csv", index=False)

# 2. Generate 50 Matching Job Descriptions (10 for each category)
print("Generating 50 matching Job Descriptions...")
jd_templates = {
    'HR': "Looking for a Human Resources generalist with experience in talent acquisition, employee relations, payroll processing, and onboarding. Must be familiar with ATS platforms and corporate compliance.",
    'ENGINEERING': "Seeking a Mechanical/Civil Engineer with strong CAD, structural analysis, and project management skills. Experience with manufacturing processes, thermodynamics, and safety protocols required.",
    'INFORMATION-TECHNOLOGY': "Hiring an IT professional or Software Engineer. Required skills include Python, AWS, Docker, machine learning pipelines, network administration, and agile methodologies.",
    'SALES': "Looking for an aggressive Sales Executive with a proven track record in B2B account management, lead generation, CRM software (Salesforce), and exceeding quarterly revenue targets.",
    'ADVOCATE': "Seeking an Associate Attorney or Legal Advocate. Must have experience in corporate litigation, contract drafting, legal research, court appearances, and client consultation. Bar admission required."
}

jd_data = []
jd_counter = 0
for category in target_categories:
    for _ in range(10):
        # Slightly alter the text so they aren't 100% identical duplicates
        text = f"{jd_templates[category]} Requisition ID: {jd_counter}."
        jd_data.append({'id': f"jd_{jd_counter}", 'text': text, 'category': category})
        jd_counter += 1

jds_clean = pd.DataFrame(jd_data)
jds_clean.to_csv("eval/jds.csv", index=False)

print("Success! eval/resumes.csv and eval/jds.csv are ready for Claude's harness.")
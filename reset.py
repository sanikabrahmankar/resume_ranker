import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

# Connect to MongoDB
db = MongoClient(os.getenv("MONGO_URI"))[os.getenv("MONGO_DB", "resume_ranker_db")]

print("Clearing database history...")
db.jobs.delete_many({})
db.candidates.delete_many({})
db.chunks.delete_many({})
print("Done! All previous jobs and candidates have been removed.")
# TenderAI recovery steps

## 1. Fix the missing templates
Copy these files into your repo:
- templates/login.html
- templates/signup.html

Redeploy if those files were missing.

## 2. Run the database migration
Run:
- migrate_auth_launch_core_postgres.sql

This adds:
- users
- user_tender_decisions
- ingest_runs.result_json
- user_id on profiles
- user_id on analysis_jobs

## 3. Redeploy the auth-enabled app
After the migration and template files are in place, redeploy App Runner.

## 4. First login flow
Once redeployed:
- open /signup
- create a new user
- upload a new profile
- browse tenders
- analyze one tender
- save a decision

## 5. Important note
Existing historical profiles and analyses will not automatically belong to the new user.
They may keep NULL user_id until you re-upload / re-analyze through the new app.

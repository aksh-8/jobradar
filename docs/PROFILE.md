# Private resume catalog

JobRadar scores only verified facts in `config/resume_profile.json`. It never modifies a resume and uses profile selection as the only resume personalization.

## Why there is one file for four resumes

Use one private catalog rather than four independent files. The `shared` object holds facts that are common to every resume, while `profiles` holds the routing information for each existing variant. This prevents employment dates and accomplishments from drifting between copies.

The configured variants are:

- `akash-biswal`: General.
- `akash-biswal-platform`: Platform.
- `akash-biswal-ai-automation`: AIAutomation.
- `akash-biswal-fde`: FDE.

Set `RESUME_PROFILE_ID=auto` so JobRadar deterministically selects the best READY variant from the job title, company, description, target roles, skills, and keywords. An explicit profile ID remains available for testing or manual overrides.

## Catalog structure

```json
{
  "shared": {
    "full_name": "Verified name",
    "status": "draft",
    "work_authorization": "Verified work-authorization statement",
    "location_preferences": ["Remote - United States"],
    "minimum_base_salary_usd": 140000,
    "preferred_base_salary_usd": 170000,
    "skills": [],
    "experience": [],
    "education": [],
    "certifications": [],
    "projects": []
  },
  "profiles": [
    {
      "profile_id": "candidate-general",
      "resume_name": "General",
      "headline": "Verified resume headline",
      "summary": "Verified role-facing summary",
      "target_roles": ["Software Engineer"],
      "target_companies": [],
      "keywords": []
    }
  ]
}
```

Dates use ISO format. Resume month-only dates are normalized to the first day of a start month and the final day of an end month. This is a storage convention, not a claim about the exact employment day.

Every `skill_names` entry under experience or projects must exactly match a top-level skill name. The loader rejects duplicate profiles, unknown skills, invalid dates, incomplete READY profiles, and unrecognized fields.

## Validate the catalog

```powershell
python -c "from backend.resume_store import configured_resume_catalog; c=configured_resume_catalog(); print([(p.profile_id, p.resume_name, p.status.value) for p in c.list_profiles()])"
```

Expected output contains all four variants with status `ready`.

Test automatic selection:

```powershell
python -c "from backend.resume_store import configured_resume_catalog,select_resume_profile; c=configured_resume_catalog(); print(select_resume_profile(c,'Platform Engineer building Kubernetes developer tooling').resume_name)"
```

Expected output: `Platform`.

Restart Uvicorn after changing the catalog because the API loads it during application startup.

## Privacy boundary

- `config/resume_profile.json`, `.env`, and `private/` are ignored by Git.
- Do not include phone numbers, street addresses, government identifiers, or references in the scoring catalog.
- Provider prompts receive the scoring context from this catalog. Include only facts you are comfortable sending to the selected scoring provider.
- Never use `git add -f` on ignored private files.

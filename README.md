# 💊 UbatJelas — Smart Prescription Medication Guide

UbatJelas helps you understand your doctor's prescription in simple, clear language.
Upload a photo of your prescription or type it in manually, and Claude Haiku 4.5 breaks
down exactly how to take your medication — including dosage, timing, frequency, and
important warnings.

---

## Architecture

```
User (Browser)
  │
  ├─► CloudFront Distribution (HTTPS)          ← frontend served here
  │     └─► S3 Bucket (private, OAC)
  │           └─► frontend/index.html
  │
  └─► Lambda Function URL (RESPONSE_STREAM)    ← AI calls made directly
        └─► MedicationInstructionsFunction
              └─► Amazon Bedrock
                    └─► Claude Haiku 4.5
                          (global cross-region inference profile)
```

**Key design decisions:**

- The Lambda uses [Lambda Web Adapter (LWA)](https://github.com/awslabs/aws-lambda-web-adapter)
  to run Flask as a streaming HTTP server inside Lambda.
- `FunctionUrlConfig.InvokeMode: RESPONSE_STREAM` enables true token-by-token streaming
  directly to the browser without buffering.
- No `Cors` block inside `FunctionUrlConfig` — it causes an `AWS::EarlyValidation`
  failure. CORS is handled by Flask response headers instead.
- The S3 bucket is **private** — CloudFront serves it via Origin Access Control (OAC).
  Never enable S3 static website hosting on the bucket directly.
- The global inference profile (`global.` prefix) routes Bedrock requests worldwide for
  maximum throughput. IAM therefore uses `*` for region in the Bedrock ARNs.

---

## Directory Structure

```
.
├── frontend/
│   └── index.html                  # Single-page app (HTML + CSS + JS)
├── functions/
│   └── medication_instructions/
│       ├── app.py                  # Flask streaming Lambda handler
│       ├── run.sh                  # LWA entrypoint (must be executable)
│       └── requirements.txt        # flask, boto3
├── infra/
│   └── template.yaml               # AWS SAM template
├── .github/
│   └── workflows/
│       └── deploy.yml              # GitHub Actions CI/CD pipeline
├── .gitignore
└── README.md
```

---

## Pre-Deployment Checklist

Complete **all** of these steps before your first push to `main`.

### 1. Enable Bedrock Model Access

1. Open [AWS Console → Amazon Bedrock → Model Access](https://console.aws.amazon.com/bedrock/home?region=ap-southeast-1#/modelaccess)
   in the **ap-southeast-1 (Singapore)** region.
2. Click **Manage model access**.
3. Find and enable:
   - **Claude Haiku 4.5** — model ID:
     `global.anthropic.claude-haiku-4-5-20251001-v1:0-20260217-v1:0`
4. First-time accounts may need to submit a use-case form. Approval is usually instant
   but can take up to a few hours.

> **Why ap-southeast-1?** The Lambda is deployed there. The `global.` inference profile
> then routes worldwide (us-east-2, us-west-2, etc.) automatically for throughput.

---

### 2. Create a SAM Deployment Bucket

SAM needs an S3 bucket in **ap-southeast-1** to store Lambda build artefacts before
deploying. Create one if you don't have one already:

```bash
aws s3 mb s3://your-sam-deploy-bucket-name --region ap-southeast-1
```

Note the bucket name — you'll need it as a secret in the next step.

---

### 3. Add GitHub Repository Secrets

Go to your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**.

Add these three secrets:

| Secret name             | Value                                                  |
|-------------------------|--------------------------------------------------------|
| `AWS_ACCESS_KEY_ID`     | Access key ID for the deploying IAM user               |
| `AWS_SECRET_ACCESS_KEY` | Secret access key for the deploying IAM user           |
| `SAM_DEPLOY_BUCKET`     | The S3 bucket name you created in step 2               |

---

### 4. IAM Permissions for the Deploying User

The IAM user / role behind the GitHub Actions credentials needs at minimum:

```json
{
  "Effect": "Allow",
  "Action": [
    "cloudformation:*",
    "s3:*",
    "lambda:*",
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
    "iam:GetRole",
    "iam:PassRole",
    "iam:TagRole",
    "cloudfront:*"
  ],
  "Resource": "*"
}
```

---

### 5. Fix `run.sh` Executable Bit (Windows users)

Git on Windows does not preserve Unix file permissions. Run this **once** after cloning
so Lambda can execute the startup script:

```bash
git update-index --chmod=+x functions/medication_instructions/run.sh
git commit -m "fix: mark run.sh as executable"
```

---

## Deploying

Push to `main` and GitHub Actions handles everything:

```bash
git push origin main
```

Watch the workflow in the **Actions** tab. When it completes, the **job summary** shows:

```
🌐 Website URL : https://xxxxxxxxxxxxxx.cloudfront.net
⚡ Lambda URL  : https://xxxxxxxxxxxxxx.lambda-url.ap-southeast-1.on.aws/
🪣 S3 Bucket   : ubatjelas-frontend-<account-id>
```

Open the **Website URL** in your browser — the app is live.

---

## Local Development

### Backend (Flask Lambda)

```bash
cd functions/medication_instructions
pip install -r requirements.txt

# Requires AWS credentials with Bedrock access in ap-southeast-1
export AWS_REGION=ap-southeast-1
python app.py
```

Test with curl:

```bash
# Text-only
curl -X POST http://localhost:8080/ \
  -H "Content-Type: application/json" \
  -d '{"prescription_text": "Amoxicillin 500mg, 3 times daily for 7 days"}' \
  --no-buffer

# With image (base64-encode a JPG first)
BASE64=$(base64 -i prescription.jpg | tr -d '\n')
curl -X POST http://localhost:8080/ \
  -H "Content-Type: application/json" \
  -d "{\"file_data\": \"${BASE64}\", \"file_mime\": \"image/jpeg\"}" \
  --no-buffer
```

### Frontend

After deploying (to get a real Lambda URL), or after manually setting the URL:

```bash
# Quick one-liner to test locally — replace with your actual Lambda URL
sed 's|__MEDICATION_INSTRUCTIONS_URL__|https://your-lambda-url.lambda-url.ap-southeast-1.on.aws/|g' \
  frontend/index.html > /tmp/index_local.html
python -m http.server 8000 --directory /tmp
# Open http://localhost:8000/index_local.html
```

---

## Stack Details

| Resource | Type | Notes |
|---|---|---|
| `AppBedrockRole` | `AWS::IAM::Role` | Lambda execution role with Bedrock invoke permissions |
| `MedicationInstructionsFunction` | `AWS::Serverless::Function` | Flask + LWA, Python 3.12, streaming |
| `FrontendBucket` | `AWS::S3::Bucket` | Private; served via CloudFront OAC only |
| `CloudFrontOAC` | `AWS::CloudFront::OriginAccessControl` | Signs S3 requests |
| `CloudFrontDistribution` | `AWS::CloudFront::Distribution` | HTTPS, PriceClass_200, custom 403/404 → index.html |

**Bedrock model:** `global.anthropic.claude-haiku-4-5-20251001-v1:0-20260217-v1:0`

---

## Disclaimer

UbatJelas is for **informational purposes only**. Always consult your doctor or
pharmacist before taking any medication. *Maklumat ini adalah untuk rujukan sahaja.
Sila rujuk doktor atau ahli farmasi anda.*

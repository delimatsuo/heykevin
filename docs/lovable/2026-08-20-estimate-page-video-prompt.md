# Lovable Prompt: Video-First AI Diagnosis & Estimate Page

This document provides the paste-ready prompt for updating the customer-facing estimate upload and result page on `heykevin.one/estimate/:token`.

---

## Lovable Prompt (Paste-Ready)

```markdown
Update the customer AI estimate upload page (`/estimate/:token`) on heykevin.one to follow a video-first diagnosis flow with polling support for asynchronous video processing.

### 1. UI & Layout (Video-First)

- **Header:**
  - Title: "Get an Instant AI Diagnosis & Estimate"
  - Subtitle: "Show us what’s going on so we can diagnose the problem and provide an accurate cost estimate."

- **Optional Text Description:**
  - An optional text field / textarea at the top:
    - Label: "Describe what's happening (optional)"
    - Placeholder: "e.g. The pipe under the kitchen sink started leaking water when running the faucet..."
    - Max length: 500 characters.

- **Primary Capture Action (Video First):**
  - Large, prominent primary button: "🎥 Record a Video"
  - Associated file input: `<input type="file" accept="video/*" capture="environment">`
  - Guidance text below button: "Record a short video (under a minute) showing the issue while explaining what happened."

- **Secondary Capture Action (Photo Fallback):**
  - Secondary button / link: "📷 Upload a photo instead"
  - Associated file input: `<input type="file" accept="image/*" capture="environment">`

- **Client-Side Validation:**
  - Before initiating upload, check file size (`file.size`):
    - If `file.size > 50 * 1024 * 1024` (50MB):
      - Block upload and display error alert: "File is too large (maximum 50MB). Please record a shorter video under a minute."

### 2. Upload and Analysis Flow

1. **Step 1: Request Upload URL**
   - Call `POST https://kevin-api-752910912062.us-central1.run.app/api/estimates/{token}/upload-url` with JSON:
     ```json
     { "content_type": file.type }
     ```
   - Receive response:
     ```json
     { "upload_url": "https://...", "max_size": 52428800, "content_type": "..." }
     ```

2. **Step 2: Upload File with Optional Description**
   - Construct target URL: Take `upload_url` from Step 1.
   - If user provided a description:
     - Append `?description=${encodeURIComponent(description.trim())}` to the upload URL.
   - Send `POST` request with raw binary body (`file`) and headers:
     - `Content-Type: ${file.type}`
   - Handle response status codes and JSON:

     - **HTTP 200 with `{ "status": "complete", "result": { ... } }` (Synchronous Photo Path):**
       - Immediately transition UI to the Diagnosis Result view.

     - **HTTP 202 with `{ "status": "processing" }` (Asynchronous Video Path):**
       - Transition UI to Processing state:
         - Heading: "Analyzing your video..."
         - Body: "Got it — we're diagnosing your issue. We'll text your estimate shortly, or you can wait here."
         - Display an animated progress/loading spinner.
       - Start polling `GET https://kevin-api-752910912062.us-central1.run.app/api/estimates/{token}` every 5 seconds for up to 3 minutes (36 attempts):
         - If response status is `"complete"`: Stop polling and render Diagnosis Result view.
         - If response status is `"failed"`: Stop polling and render Processing Error view.
         - If timeout reached (3 minutes): Stop polling and show: "We are still analyzing your video. We will text you the full estimate and diagnosis as soon as it's ready!"

     - **HTTP 409 Conflict (`{ "status": "processing" }`):**
       - Show alert: "We're already working on your last upload. Please wait a moment for the result."

     - **HTTP 413 Payload Too Large:**
       - Show alert: "File is too large. Please record a shorter video."

     - **HTTP 429 Too Many Requests:**
       - Show alert: "Upload limit reached for this estimate link."

     - **HTTP 404 / 403 / 5xx:**
       - Show alert: "Unable to process upload. Please try again or call the business directly."

### 3. Diagnosis Result View (`complete`)

- If `result.requires_manual_investigation` is true:
  - Header: "Manual Inspection Required"
  - Body: "Thanks for your upload. This issue will require a technician to inspect in person to give an accurate quote."
  - Contact button: "Call Business" with `tel:` link.

- If diagnosis is available:
  - Diagnosis Card:
    - Title: "AI Diagnosis"
    - Body: `${result.diagnosis}`
  - Estimated Cost Card:
    - Price Range: `$${result.estimate_min} - $${result.estimate_max}`
    - Disclaimer: "⚠️ This is an AI-generated preliminary estimate. Actual cost may differ based on the technician's hands-on diagnosis."
  - Matched Services List:
    - Render matched services from `result.matched_services`.
  - Contact action: "Call to Schedule" with `tel:` link.

### 4. Failure View (`failed`)

- Heading: "We couldn't analyze this video"
- Body: "We were unable to process the video details. Please call the business directly to describe your issue and book an appointment."
- Button: "Call Business"
```

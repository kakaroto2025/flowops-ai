const apiStatus = document.querySelector("#apiStatus");
const runDemoBtn = document.querySelector("#runDemoBtn");
const resetBtn = document.querySelector("#resetBtn");
const pdfInput = document.querySelector("#pdfInput");
const processingRegion = document.querySelector("#processingRegion");
const processUploadBtn = document.querySelector("#processUploadBtn");
const selectedFiles = document.querySelector("#selectedFiles");
const jobMeta = document.querySelector("#jobMeta");
const jobStatus = document.querySelector("#jobStatus");
const documentsProcessed = document.querySelector("#documentsProcessed");
const successRate = document.querySelector("#successRate");
const erpRecords = document.querySelector("#erpRecords");
const humanReviews = document.querySelector("#humanReviews");
const documentsTable = document.querySelector("#documentsTable");
const reviewList = document.querySelector("#reviewList");
const eventsList = document.querySelector("#eventsList");
const erpTable = document.querySelector("#erpTable");
const jobsHistoryTable = document.querySelector("#jobsHistoryTable");
const finopsMode = document.querySelector("#finopsMode");
const finopsDocsToday = document.querySelector("#finopsDocsToday");
const finopsGeminiToday = document.querySelector("#finopsGeminiToday");
const finopsTokensToday = document.querySelector("#finopsTokensToday");
const finopsCostToday = document.querySelector("#finopsCostToday");
const finopsBudgetUsage = document.querySelector("#finopsBudgetUsage");

let currentJobId = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function money(value, currency = "USD") {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: currency || "USD",
  }).format(value || 0);
}

function dateTime(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function dateFull(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("en-US", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

async function request(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

function setLoading(isLoading) {
  runDemoBtn.disabled = isLoading;
  resetBtn.disabled = isLoading;
  processUploadBtn.disabled = isLoading || !pdfInput.files.length;
  runDemoBtn.textContent = isLoading ? "Processing..." : "Run Demo";
  processUploadBtn.textContent = isLoading ? "Processing..." : "Process Upload";
}

function setApiStatus(ok) {
  apiStatus.className = ok ? "status ok" : "status error";
  apiStatus.textContent = ok ? "API online" : "API offline";
}

function selectedPdfFiles() {
  return Array.from(pdfInput.files).sort((left, right) =>
    left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: "base" }),
  );
}

function renderDashboard(data) {
  currentJobId = data.job.id;
  documentsProcessed.textContent = data.kpis.documents_processed;
  successRate.textContent = `${data.kpis.success_rate}%`;
  jobMeta.textContent = `${data.job.id} | ${data.job.document_count} documents | ${data.job.processing_region || "AUTO"} | ${dateTime(data.job.updated_at)}`;
  jobStatus.textContent = data.job.status;
  jobStatus.className = data.job.status === "COMPLETED" ? "pill done" : "pill muted";

  renderDocuments(data.documents);
  renderEvents(data.recent_events);
}

function renderJobHistory(jobs) {
  if (!jobs.length) {
    jobsHistoryTable.innerHTML = `<tr><td colspan="7" class="empty">No jobs processed.</td></tr>`;
    return;
  }

  jobsHistoryTable.innerHTML = jobs
    .map((job) => {
      const selected = job.job_id === currentJobId ? " selected" : "";
      return `
        <tr class="history-row${selected}" data-job-id="${escapeHtml(job.job_id)}">
          <td>${escapeHtml(job.job_id)}</td>
          <td>${dateFull(job.updated_at || job.created_at)}</td>
          <td>${job.document_count}</td>
          <td><span class="status-text status-${String(job.status).toLowerCase()}">${escapeHtml(job.status)}</span></td>
          <td>${job.approved}</td>
          <td>${job.human_reviews_open ?? job.human_reviews}/${job.human_reviews_total ?? job.human_reviews}</td>
          <td>${job.rejected}</td>
        </tr>
      `;
    })
    .join("");
}

async function refreshJobHistory() {
  const jobs = await request("/jobs");
  renderJobHistory(jobs);
}

async function refreshGlobalReviews() {
  const reviews = await request("/human-reviews");
  renderReviews(reviews);
  humanReviews.textContent = reviews.length;
}

async function refreshGlobalErp() {
  const records = await request("/erp-records");
  renderErp(records);
  erpRecords.textContent = records.length;
}

async function refreshFinops() {
  const usage = await request("/api/finops/usage");
  finopsMode.textContent = usage.free_tier_first ? "FREE TIER FIRST: ON" : "FREE TIER FIRST: OFF";
  finopsMode.className = usage.free_tier_first ? "pill done" : "pill muted";
  finopsDocsToday.textContent = usage.documents_today;
  finopsGeminiToday.textContent = usage.gemini_calls_today;
  const tokens = [usage.input_tokens_today, usage.output_tokens_today].filter((value) => value !== null && value !== undefined);
  finopsTokensToday.textContent = tokens.length ? tokens.reduce((sum, value) => sum + value, 0) : "--";
  finopsCostToday.textContent = usage.estimated_ai_cost_today_usd === null
    ? "--"
    : money(usage.estimated_ai_cost_today_usd, "USD");
  finopsBudgetUsage.textContent = usage.percentage_of_internal_budget_used === null
    ? "--"
    : `${usage.percentage_of_internal_budget_used}%`;
}

async function loadJob(jobId) {
  setLoading(true);
  try {
    const data = await request(`/jobs/${jobId}/dashboard`);
    renderDashboard(data);
    await refreshJobHistory();
    await refreshGlobalReviews();
    await refreshGlobalErp();
    await refreshFinops();
    setApiStatus(true);
  } catch (error) {
    setApiStatus(false);
    jobMeta.textContent = `Job loading error: ${error.message}`;
  } finally {
    setLoading(false);
  }
}

function renderDocuments(documents) {
  if (!documents.length) {
    documentsTable.innerHTML = `<tr><td colspan="5" class="empty">No documents.</td></tr>`;
    return;
  }

  documentsTable.innerHTML = documents
    .map((doc) => {
      const statusClass = `status-${doc.status.toLowerCase()}`;
      return `
        <tr>
          <td>${doc.file_name}</td>
          <td><span class="status-text ${statusClass}">${doc.status}</span></td>
          <td>${doc.retry_count}</td>
          <td>${doc.country_code || doc.processing_region || "--"}</td>
          <td>${doc.id}</td>
        </tr>
      `;
    })
    .join("");
}

function renderReviews(reviews) {
  const openReviews = reviews.filter((review) => review.status === "OPEN");
  if (!openReviews.length) {
    reviewList.innerHTML = `<div class="empty-card">No open reviews.</div>`;
    return;
  }

  reviewList.innerHTML = openReviews
    .map((review) => {
      const fields = review.suggested_fields || {};
      return `
        <div class="review-card review-form" data-review-id="${escapeHtml(review.id)}">
          <div class="review-title">
            <strong>${escapeHtml(review.document_id)}</strong>
            <span>${escapeHtml(review.job_id)} | ${escapeHtml(review.file_name || "no file")}</span>
            <span>${escapeHtml(review.reason)}</span>
          </div>
          <label>
            <span>Country</span>
            <input data-field="country_code" value="${escapeHtml(fields.country_code)}" />
          </label>
          <label>
            <span>Company</span>
            <input data-field="company_name" value="${escapeHtml(fields.company_name)}" />
          </label>
          <label>
            <span>Tax ID Type</span>
            <input data-field="tax_id_type" value="${escapeHtml(fields.tax_id_type || (fields.cnpj ? "CNPJ" : ""))}" />
          </label>
          <label>
            <span>Tax ID</span>
            <input data-field="tax_id" value="${escapeHtml(fields.tax_id || fields.cnpj)}" />
          </label>
          <label>
            <span>Invoice Number</span>
            <input data-field="invoice_number" value="${escapeHtml(fields.invoice_number)}" />
          </label>
          <label>
            <span>Issue Date</span>
            <input data-field="issue_date" value="${escapeHtml(fields.issue_date)}" />
          </label>
          <label>
            <span>Total Amount</span>
            <input data-field="total_amount" value="${escapeHtml(fields.total_amount)}" inputmode="decimal" />
          </label>
          <label>
            <span>Currency</span>
            <input data-field="currency" value="${escapeHtml(fields.currency)}" />
          </label>
          <div class="review-actions">
            <button class="button primary" type="button" data-action="approve-review">Correct and Approve</button>
            <button class="button danger" type="button" data-action="reject-review">Reject</button>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderEvents(events) {
  if (!events.length) {
    eventsList.innerHTML = `<div class="empty-card">No events yet.</div>`;
    return;
  }

  eventsList.innerHTML = events
    .slice()
    .reverse()
    .map((event) => {
      const type = event.event_type.toLowerCase();
      const className = type.includes("approved")
        ? "event-card success"
        : type.includes("review")
          ? "event-card review"
          : type.includes("decision")
            ? "event-card decision"
            : "event-card";
      return `
        <div class="${className}">
          <strong>${event.agent} | ${event.event_type}</strong>
          <span>${dateTime(event.created_at)} | ${event.message}</span>
        </div>
      `;
    })
    .join("");
}

function renderErp(records) {
  if (!records.length) {
    erpTable.innerHTML = `<tr><td colspan="6" class="empty">No records sent.</td></tr>`;
    return;
  }

  erpTable.innerHTML = records
    .map(
      (record) => `
        <tr>
          <td>${record.invoice_number}</td>
          <td>${record.tax_id || record.cnpj}</td>
          <td>${record.tax_id_type || (record.cnpj ? "CNPJ" : "--")}</td>
          <td>${record.country_code || "--"}</td>
          <td>${money(record.total_amount, record.currency || "USD")}</td>
          <td>${record.currency || "--"}</td>
        </tr>
      `,
    )
    .join("");
}

async function runDemo() {
  setLoading(true);
  try {
    const region = processingRegion?.value || "AUTO";
    const data = await request(`/jobs/demo/run?processing_region=${encodeURIComponent(region)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    renderDashboard(data);
    await refreshJobHistory();
    await refreshGlobalReviews();
    await refreshGlobalErp();
    await refreshFinops();
    setApiStatus(true);
  } catch (error) {
    setApiStatus(false);
    jobMeta.textContent = `Demo processing error: ${error.message}`;
  } finally {
    setLoading(false);
  }
}

async function resetDemo() {
  setLoading(true);
  try {
    await request("/dev/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    currentJobId = null;
    documentsProcessed.textContent = "--";
    successRate.textContent = "--";
    erpRecords.textContent = "--";
    humanReviews.textContent = "--";
    jobMeta.textContent = "No job loaded";
    jobStatus.textContent = "waiting";
    jobStatus.className = "pill muted";
    renderDocuments([]);
    renderReviews([]);
    renderEvents([]);
    renderErp([]);
    renderJobHistory([]);
    await refreshFinops();
    setApiStatus(true);
  } catch (error) {
    setApiStatus(false);
  } finally {
    setLoading(false);
  }
}

async function processUpload() {
  if (!pdfInput.files.length) return;

  const payload = new FormData();
  selectedPdfFiles().forEach((file) => payload.append("files", file));
  payload.append("processing_region", processingRegion?.value || "AUTO");

  setLoading(true);
  try {
    const data = await request("/jobs/upload/run", {
      method: "POST",
      body: payload,
    });
    renderDashboard(data);
    await refreshJobHistory();
    await refreshGlobalReviews();
    await refreshGlobalErp();
    await refreshFinops();
    setApiStatus(true);
  } catch (error) {
    setApiStatus(false);
    jobMeta.textContent = `Upload processing error: ${error.message}`;
  } finally {
    setLoading(false);
  }
}

function parseAmount(value) {
  let cleaned = String(value || "").replace(/[^0-9,.\-]/g, "");
  if (cleaned.includes(",") && cleaned.includes(".")) {
    cleaned = cleaned.lastIndexOf(",") > cleaned.lastIndexOf(".")
      ? cleaned.replace(/\./g, "").replace(",", ".")
      : cleaned.replace(/,/g, "");
  } else if (cleaned.includes(",")) {
    cleaned = cleaned.replace(/\./g, "").replace(",", ".");
  }
  const amount = Number(cleaned);
  return Number.isFinite(amount) ? amount : value;
}

async function approveReview(card) {
  const reviewId = card.dataset.reviewId;
  const correctedFields = {};
  card.querySelectorAll("[data-field]").forEach((input) => {
    const key = input.dataset.field;
    correctedFields[key] = key === "total_amount" ? parseAmount(input.value) : input.value.trim();
  });

  setLoading(true);
  try {
    const data = await request(`/human-reviews/${reviewId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ corrected_fields: correctedFields, reviewer: "operator" }),
    });
    renderDashboard(data);
    await refreshJobHistory();
    await refreshGlobalReviews();
    await refreshGlobalErp();
    await refreshFinops();
    setApiStatus(true);
  } catch (error) {
    setApiStatus(false);
    jobMeta.textContent = `Review approval error: ${error.message}`;
  } finally {
    setLoading(false);
  }
}

async function rejectReview(card) {
  const reviewId = card.dataset.reviewId;

  setLoading(true);
  try {
    const data = await request(`/human-reviews/${reviewId}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewer: "operator" }),
    });
    renderDashboard(data);
    await refreshJobHistory();
    await refreshGlobalReviews();
    await refreshGlobalErp();
    await refreshFinops();
    setApiStatus(true);
  } catch (error) {
    setApiStatus(false);
    jobMeta.textContent = `Review rejection error: ${error.message}`;
  } finally {
    setLoading(false);
  }
}

function renderSelectedFiles() {
  const files = selectedPdfFiles();
  processUploadBtn.disabled = files.length === 0;
  if (!files.length) {
    selectedFiles.textContent = "No files selected.";
    return;
  }
  selectedFiles.innerHTML = files.map((file) => `<span>${file.name}</span>`).join("");
}

async function boot() {
  try {
    await request("/health");
    await refreshJobHistory();
    await refreshGlobalReviews();
    await refreshGlobalErp();
    await refreshFinops();
    setApiStatus(true);
  } catch (error) {
    setApiStatus(false);
  }
}

runDemoBtn.addEventListener("click", runDemo);
resetBtn.addEventListener("click", resetDemo);
processUploadBtn.addEventListener("click", processUpload);
pdfInput.addEventListener("change", renderSelectedFiles);
reviewList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const card = button.closest(".review-form");
  if (!card) return;
  if (button.dataset.action === "approve-review") {
    approveReview(card);
  }
  if (button.dataset.action === "reject-review") {
    rejectReview(card);
  }
});
jobsHistoryTable.addEventListener("click", (event) => {
  const row = event.target.closest("[data-job-id]");
  if (!row) return;
  loadJob(row.dataset.jobId);
});
boot();

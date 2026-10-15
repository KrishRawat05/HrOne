const root = document.querySelector("#view-root");
const dialog = document.querySelector("#action-dialog");
const dialogContent = document.querySelector("#dialog-content");
const toastRegion = document.querySelector("#toast-region");
const serviceLabel = document.querySelector("#service-label");
const serviceDot = document.querySelector(".service-dot");
const PAGE_SIZE = 20;
const state = { view: "overview", attendancePage: 1, employeePage: 1, employees: [], attendanceFilters: {} };

function dateIST(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(date);
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function monthNow() { return dateIST().slice(0, 7); }
function escapeHTML(value = "") {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}
function formatTime(milliseconds) {
  if (!milliseconds) return "—";
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(milliseconds));
}
function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).format(new Date(`${value}T00:00:00Z`));
}
function queryString(values) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== "" && value != null) params.set(key, value);
  return params.toString();
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
  return data;
}
function notify(message, isError = false) {
  const toast = document.createElement("div");
  toast.className = `toast${isError ? " is-error" : ""}`;
  toast.textContent = message;
  toastRegion.append(toast);
  window.setTimeout(() => toast.remove(), 4200);
}
function setService(online) {
  serviceLabel.textContent = online ? "API connected" : "API unavailable";
  serviceDot.classList.toggle("is-online", online);
  serviceDot.classList.toggle("is-offline", !online);
}
function statusBadge(status, open = false) {
  const safe = escapeHTML(status || "UNKNOWN").toLowerCase();
  return `<span class="badge badge-${safe}">${escapeHTML(status || "UNKNOWN")}</span>${open ? ' <span class="badge badge-open">OPEN</span>' : ""}`;
}
function emptyState(title, message, button = "") {
  return `<div class="empty-state"><strong>${escapeHTML(title)}</strong>${escapeHTML(message)}${button}</div>`;
}
function pageHeading(eyebrow, title, note, action = "") {
  return `<div class="page-heading"><div><p class="eyebrow">${escapeHTML(eyebrow)}</p><h1>${escapeHTML(title)}</h1><p class="heading-note">${escapeHTML(note)}</p></div>${action}</div>`;
}
function tableMarkup(headers, rows, emptyTitle, emptyMessage, action = "") {
  if (!rows.length) return emptyState(emptyTitle, emptyMessage, action);
  return `<div class="table-wrap"><table><thead><tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}
function employeeRows(items) {
  return items.map((employee) => `<tr>
    <td class="code-cell">${escapeHTML(employee.emp_code)}</td>
    <td class="name-cell"><strong>${escapeHTML(employee.name)}</strong><span>${escapeHTML(employee.email)}</span></td>
    <td>${escapeHTML(employee.department)}</td>
    <td>${escapeHTML(employee.shift_start)}–${escapeHTML(employee.shift_end)}<span class="sub-cell">IST</span></td>
    <td>${formatDate(employee.joined_on)}</td>
  </tr>`);
}
function attendanceRows(items, allowActions = true) {
  return items.map((record) => {
    const open = record.punch_in != null && record.punch_out == null && ["PRESENT", "WFH", "ON_DUTY"].includes(record.status);
    const actions = allowActions ? `<div class="table-actions">${open ? `<button class="button button-secondary button-small" data-action="punch-out" data-employee="${escapeHTML(record.emp_code)}">Punch out</button>` : ""}<button class="button button-quiet button-small" data-action="regularize" data-employee="${escapeHTML(record.emp_code)}" data-date="${escapeHTML(record.date)}">Correct</button></div>` : "";
    return `<tr>
      <td>${formatDate(record.date)}</td><td class="code-cell">${escapeHTML(record.emp_code)}</td>
      <td>${statusBadge(record.status, open)}</td><td>${formatTime(record.punch_in)}</td><td>${formatTime(record.punch_out)}</td>
      <td>${record.work_hours == null ? "—" : `${Number(record.work_hours).toFixed(2)} h`}</td><td>${Number(record.late_minutes || 0)} min</td>
      ${allowActions ? `<td>${actions}</td>` : ""}
    </tr>`;
  });
}

async function loadOverview() {
  const today = dateIST();
  const month = today.slice(0, 7);
  const [employees, attendance, departments] = await Promise.all([
    api("/employees?page=1&page_size=100"),
    api(`/attendance?${queryString({ date_from: today, date_to: today, page: 1, page_size: 100 })}`),
    api(`/analytics/departments/summary?month=${month}`),
  ]);
  state.employees = employees.items;
  const present = attendance.items.filter((item) => ["PRESENT", "WFH", "ON_DUTY"].includes(item.status)).length;
  const late = attendance.items.filter((item) => Number(item.late_minutes) > 0).length;
  const open = attendance.items.filter((item) => item.punch_in && !item.punch_out && ["PRESENT", "WFH", "ON_DUTY"].includes(item.status)).length;
  const topDepartments = departments.items.slice(0, 5).map((item) => `<div class="bar-row"><span>${escapeHTML(item.department)}</span><span class="bar-track"><span class="bar-fill" style="display:block;width:${Math.min(100, Math.max(4, item.headcount / Math.max(1, ...departments.items.map((row) => row.headcount)) * 100))}%"></span></span><strong>${item.headcount}</strong></div>`).join("");
  root.innerHTML = `<div class="fade-in">
    ${pageHeading("THURSDAY / DAILY BRIEF", "Good morning.", `Here is the workforce picture for ${formatDate(today)}.`, '<button class="button" data-action="new-punch">＋ Record punch</button>')}
    <section class="metric-strip" aria-label="Today's key figures">
      <div class="metric"><span class="metric-label">People on roster</span><strong class="metric-value">${employees.total}</strong><div class="metric-foot">Across ${departments.items.length} departments</div></div>
      <div class="metric"><span class="metric-label">Presence today</span><strong class="metric-value">${present}<span class="metric-foot"> / ${employees.total}</span></strong><div class="metric-foot">Present, WFH, or on duty</div></div>
      <div class="metric"><span class="metric-label">Late arrivals</span><strong class="metric-value">${late}</strong><div class="metric-foot">Records beyond grace window</div></div>
      <div class="metric"><span class="metric-label">Open shifts</span><strong class="metric-value">${open}</strong><div class="metric-foot">Waiting for punch-out</div></div>
    </section>
    <div class="split-layout">
      <section class="plain-section"><div class="section-row"><div><h2>Today's attendance</h2><p>Recent punches from the daily register</p></div><button class="button button-quiet button-small" data-navigate="attendance">Open register →</button></div>
        ${tableMarkup(["Date", "Employee", "Status", "In", "Out", "Hours", "Late", ""], attendanceRows(attendance.items.slice(0, 8)), "No punches recorded", "The register is clear for today. Add an employee's first punch when they're ready.", '<button class="button button-small" data-action="new-punch">＋ Record punch</button>')}
      </section>
      <aside class="side-note"><p class="eyebrow">ROSTER SHAPE</p><h3>Teams at a glance</h3><p>Current monthly headcount includes employees who joined by the end of this month, including those without attendance records.</p><div class="bar-list">${topDepartments || '<p class="result-placeholder">Department summaries appear when employees are registered.</p>'}</div><span class="note-value">${employees.total}<small class="metric-foot"> total employees</small></span></aside>
    </div>
  </div>`;
}

async function loadEmployees() {
  const data = await api(`/employees?${queryString({ page: state.employeePage, page_size: PAGE_SIZE, department: state.employeeDepartment })}`);
  state.employees = data.items;
  const action = '<button class="button" data-action="new-employee">＋ Add employee</button>';
  const rows = employeeRows(data.items);
  root.innerHTML = `<div class="fade-in">
    ${pageHeading("PEOPLE DIRECTORY", "Employees", "Manage employee profiles, teams, and shift schedules.", action)}
    <form class="toolbar" id="employee-filter"><div class="field field-grow"><label for="employee-search">Search this page</label><input id="employee-search" name="search" placeholder="Name, code, or email"></div><div class="field"><label for="department-filter">Department</label><input id="department-filter" name="department" value="${escapeHTML(state.employeeDepartment || "")}" placeholder="All departments"></div><button class="button button-secondary" type="submit">Filter</button><button class="button button-quiet" type="button" data-action="clear-employee-filter">Clear</button></form>
    <div id="employee-table">${tableMarkup(["Employee code", "Name / email", "Department", "Shift", "Joined"], rows, "No employees yet", "Create the first employee profile to begin recording attendance.", '<button class="button button-small" data-action="new-employee">＋ Add employee</button>')}</div>
    <div class="pagination"><span>${data.total} employees · page ${data.page}</span><div class="pagination-actions"><button class="button button-secondary button-small" data-action="employee-prev" ${data.page <= 1 ? "disabled" : ""}>← Previous</button><button class="button button-secondary button-small" data-action="employee-next" ${data.items.length < PAGE_SIZE ? "disabled" : ""}>Next →</button></div></div>
  </div>`;
  document.querySelector("#employee-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    state.employeeDepartment = new FormData(event.currentTarget).get("department").trim();
    state.employeePage = 1;
    loadEmployees().catch(showPageError);
  });
  document.querySelector("#employee-search").addEventListener("input", (event) => {
    const term = event.target.value.trim().toLowerCase();
    const filtered = data.items.filter((item) => `${item.emp_code} ${item.name} ${item.email}`.toLowerCase().includes(term));
    document.querySelector("#employee-table").innerHTML = tableMarkup(["Employee code", "Name / email", "Department", "Shift", "Joined"], employeeRows(filtered), "No matches", "Try a different name, email, or employee code.");
  });
}

async function loadAttendance() {
  const today = dateIST();
  const filters = state.attendanceFilters;
  const params = { ...filters, page: state.attendancePage, page_size: PAGE_SIZE };
  const data = await api(`/attendance?${queryString(params)}`);
  const rows = attendanceRows(data.items);
  root.innerHTML = `<div class="fade-in">
    ${pageHeading("DAILY REGISTER", "Attendance", "Search the attendance ledger and resolve open shifts.", '<button class="button" data-action="new-punch">＋ Record punch</button>')}
    <form class="toolbar" id="attendance-filter">
      <div class="field"><label for="filter-employee">Employee code</label><input id="filter-employee" name="emp_code" value="${escapeHTML(filters.emp_code || "")}" placeholder="EMP0001"></div>
      <div class="field"><label for="filter-from">From</label><input id="filter-from" name="date_from" type="date" value="${escapeHTML(filters.date_from || today)}"></div>
      <div class="field"><label for="filter-to">To</label><input id="filter-to" name="date_to" type="date" value="${escapeHTML(filters.date_to || today)}"></div>
      <div class="field"><label for="filter-status">Status</label><select id="filter-status" name="status"><option value="">All statuses</option>${["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"].map((status) => `<option ${filters.status === status ? "selected" : ""}>${status}</option>`).join("")}</select></div>
      <button class="button button-secondary" type="submit">Apply filters</button><button class="button button-quiet" type="button" data-action="clear-attendance-filter">Reset</button>
    </form>
    ${tableMarkup(["Date", "Employee", "Status", "Punch in", "Punch out", "Hours", "Late", "Actions"], rows, "No attendance found", "Adjust the date range or record a punch to populate the ledger.", '<button class="button button-small" data-action="new-punch">＋ Record punch</button>')}
    <div class="pagination"><span>${data.total} matching records · page ${data.page}</span><div class="pagination-actions"><button class="button button-secondary button-small" data-action="attendance-prev" ${data.page <= 1 ? "disabled" : ""}>← Previous</button><button class="button button-secondary button-small" data-action="attendance-next" ${data.items.length < PAGE_SIZE ? "disabled" : ""}>Next →</button></div></div>
  </div>`;
  document.querySelector("#attendance-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    state.attendanceFilters = Object.fromEntries(new FormData(event.currentTarget).entries());
    state.attendancePage = 1;
    loadAttendance().catch(showPageError);
  });
}

async function loadAnalytics() {
  const employees = await api("/employees?page=1&page_size=100");
  state.employees = employees.items;
  const departments = [...new Set(employees.items.map((employee) => employee.department))].sort();
  root.innerHTML = `<div class="fade-in">
    ${pageHeading("MEASUREMENT", "Analytics", "Monthly performance and daily team trends from recorded attendance.")}
    <div class="analytics-grid">
      <section class="analytics-block"><div class="section-row"><div><h2>Employee month</h2><p>Attendance, punctuality, and overtime</p></div></div>
        <form class="inline-form" id="employee-month-form"><div class="field"><label for="monthly-employee">Employee code</label><input id="monthly-employee" name="emp_code" placeholder="EMP0001" required list="employee-codes"><datalist id="employee-codes">${employees.items.map((employee) => `<option value="${escapeHTML(employee.emp_code)}">${escapeHTML(employee.name)}</option>`).join("")}</datalist></div><div class="field"><label for="monthly-month">Month</label><input id="monthly-month" name="month" type="month" value="${monthNow()}" required></div><button class="button button-small">View</button></form><div id="employee-month-result" class="result-placeholder">Choose an employee and month to load their summary.</div>
      </section>
      <section class="analytics-block"><div class="section-row"><div><h2>Late leaderboard</h2><p>Ranked by total late minutes</p></div></div>
        <form class="inline-form" id="leaderboard-form"><div class="field"><label for="leader-month">Month</label><input id="leader-month" name="month" type="month" value="${monthNow()}" required></div><div class="field"><label for="leader-department">Department</label><select id="leader-department" name="department"><option value="">All teams</option>${departments.map((department) => `<option>${escapeHTML(department)}</option>`).join("")}</select></div><button class="button button-small">Rank</button></form><div id="leaderboard-result" class="result-placeholder">Run the leaderboard to compare late minutes.</div>
      </section>
      <section class="analytics-block analytics-block-wide"><div class="section-row"><div><h2>Department summary</h2><p>Headcount includes employees with no attendance records.</p></div></div>
        <form class="inline-form" id="department-summary-form"><div class="field"><label for="summary-month">Month</label><input id="summary-month" name="month" type="month" value="${monthNow()}" required></div><div class="field"><label for="summary-department">Department</label><select id="summary-department" name="department"><option value="">All teams</option>${departments.map((department) => `<option>${escapeHTML(department)}</option>`).join("")}</select></div><button class="button button-small">Load summary</button></form><div id="department-summary-result" class="result-placeholder">Load the monthly department summary.</div>
      </section>
      <section class="analytics-block analytics-block-wide"><div class="section-row"><div><h2>Daily trend</h2><p>Attendance rate by working day, with a seven-day trailing average.</p></div></div>
        <form class="inline-form" id="trend-form"><div class="field"><label for="trend-department">Department</label><select id="trend-department" name="department" required><option value="">Choose team</option>${departments.map((department) => `<option>${escapeHTML(department)}</option>`).join("")}</select></div><div class="field"><label for="trend-from">From</label><input id="trend-from" name="from" type="date" value="${dateIST(new Date(Date.now() - 13 * 86400000))}" required></div><div class="field"><label for="trend-to">To</label><input id="trend-to" name="to" type="date" value="${dateIST()}" required></div><button class="button button-small">Show trend</button></form><div id="trend-result" class="result-placeholder">Select a team and a date range of up to 92 days.</div>
      </section>
    </div>
  </div>`;
  document.querySelector("#employee-month-form").addEventListener("submit", (event) => runAnalyticsForm(event, async (values) => {
    const data = await api(`/analytics/employees/${encodeURIComponent(values.emp_code)}/monthly?month=${encodeURIComponent(values.month)}`);
    document.querySelector("#employee-month-result").innerHTML = `<div class="summary-result">${[["Present days", data.present_days], ["Working days", data.working_days], ["Attendance", data.attendance_pct == null ? "—" : `${data.attendance_pct}%`], ["Leave days", data.leave_days], ["Late arrivals", data.late_count], ["Late minutes", data.total_late_minutes], ["Overtime", `${data.total_overtime_minutes} min`]].map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join("")}</div>`;
  }));
  document.querySelector("#leaderboard-form").addEventListener("submit", (event) => runAnalyticsForm(event, async (values) => {
    const data = await api(`/analytics/leaderboard/late?${queryString(values)}`);
    const rows = data.items.map((item) => `<tr><td class="mono">#${item.rank}</td><td class="code-cell">${escapeHTML(item.emp_code)}</td><td class="name-cell"><strong>${escapeHTML(item.name)}</strong><span>${escapeHTML(item.department)}</span></td><td>${item.total_late_minutes} min</td><td>${item.late_count}</td></tr>`);
    document.querySelector("#leaderboard-result").innerHTML = tableMarkup(["Rank", "Employee", "Name / team", "Late total", "Late days"], rows, "No late arrivals", "No employees recorded late punches for this selection.");
  }));
  document.querySelector("#department-summary-form").addEventListener("submit", (event) => runAnalyticsForm(event, async (values) => {
    const data = await api(`/analytics/departments/summary?${queryString(values)}`);
    const rows = data.items.map((item) => `<tr><td><strong>${escapeHTML(item.department)}</strong></td><td>${item.headcount}</td><td>${item.present_days}</td><td>${item.avg_work_hours == null ? "—" : `${item.avg_work_hours} h`}</td><td>${item.late_count}</td><td>${item.leave_count}</td><td>${item.on_duty_count}</td></tr>`);
    document.querySelector("#department-summary-result").innerHTML = tableMarkup(["Department", "People", "Present days", "Avg. hours", "Late", "Leave", "On duty"], rows, "No departments for this month", "Try another month or register employees.");
  }));
  document.querySelector("#trend-form").addEventListener("submit", (event) => runAnalyticsForm(event, async (values) => {
    const department = encodeURIComponent(values.department);
    const data = await api(`/analytics/departments/${department}/trend?${queryString({ from: values.from, to: values.to })}`);
    const max = Math.max(1, ...data.items.map((item) => Number(item.attendance_rate || 0)));
    document.querySelector("#trend-result").innerHTML = `<div class="trend-list" aria-label="Daily attendance rate">${data.items.map((item) => {
      const rate = item.attendance_rate == null ? null : Number(item.attendance_rate) * 100;
      const height = rate == null ? 3 : Math.max(4, rate / max * 100);
      return `<div class="trend-day${item.is_working_day ? "" : " is-weekend"}" title="${escapeHTML(item.date)} · ${rate == null ? "No rate" : `${rate.toFixed(1)}%`} · ${item.present_count} present of ${item.headcount}"><span>${rate == null ? "—" : `${rate.toFixed(0)}%`}</span><div class="trend-bar" style="height:${height}%"></div><small>${escapeHTML(item.date.slice(5))}</small></div>`;
    }).join("")}</div><p class="result-placeholder">Bars show daily attendance rate. Hover a day for headcount and present totals.</p>`;
  }));
}

async function runAnalyticsForm(event, action) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit'], button:not([type])");
  if (submit) submit.disabled = true;
  try { await action(Object.fromEntries(new FormData(form).entries())); }
  catch (error) { notify(error.message, true); }
  finally { if (submit) submit.disabled = false; }
}

function showPageError(error) {
  root.innerHTML = `<div class="fade-in">${pageHeading("SERVICE RESPONSE", "Could not load this view", "The request did not complete. Check the API connection and try again.")}<div class="side-note"><h3>Details</h3><p>${escapeHTML(error.message)}</p><button class="button button-secondary button-small" data-action="retry">Retry</button></div></div>`;
  notify(error.message, true);
}

async function renderView(view = state.view) {
  state.view = view;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view));
  document.querySelector("#current-section").textContent = ({ overview: "Overview", attendance: "Attendance", employees: "Employees", analytics: "Analytics" })[view];
  root.innerHTML = '<div class="loading-state"><span class="loader"></span>Loading workforce data</div>';
  try {
    if (view === "overview") await loadOverview();
    else if (view === "employees") await loadEmployees();
    else if (view === "attendance") await loadAttendance();
    else await loadAnalytics();
    setService(true);
  } catch (error) { setService(false); showPageError(error); }
  root.focus({ preventScroll: true });
}

function openDialog(title, description, content) {
  dialogContent.innerHTML = `<div class="dialog-heading"><p class="eyebrow">WORKFORCE ACTION</p><h2 id="dialog-title">${escapeHTML(title)}</h2><p>${escapeHTML(description)}</p></div>${content}`;
  dialog.showModal();
}
function closeDialog() { dialog.close(); dialogContent.innerHTML = ""; }
function formError(form, message) {
  let node = form.querySelector(".form-error");
  if (!node) { node = document.createElement("p"); node.className = "form-error"; node.setAttribute("role", "alert"); form.append(node); }
  node.textContent = message;
}
function dialogForm(markup, handler) {
  openDialog(markup.title, markup.description, `<form id="dialog-form">${markup.fields}<div class="dialog-actions"><button type="button" class="button button-secondary" data-action="close-dialog">Cancel</button><button class="button">${escapeHTML(markup.submit || "Save")}</button></div></form>`);
  document.querySelector("#dialog-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector("button[type='submit'], button:not([type])");
    if (submit) submit.disabled = true;
    try { await handler(Object.fromEntries(new FormData(form).entries()), form); }
    catch (error) { formError(form, error.message); if (submit) submit.disabled = false; }
  });
}
function openEmployeeDialog() {
  dialogForm({ title: "Add employee", description: "Create a profile before recording their attendance.", submit: "Create profile", fields: `<div class="form-grid">
    <div class="field"><label>Employee code</label><input name="emp_code" placeholder="EMP0001" pattern="EMP[0-9]{4,6}" required></div>
    <div class="field"><label>Full name</label><input name="name" maxlength="100" required></div>
    <div class="field field-wide"><label>Email</label><input name="email" type="email" maxlength="120" required></div>
    <div class="field"><label>Department</label><input name="department" maxlength="50" required></div>
    <div class="field"><label>Joined on</label><input name="joined_on" type="date" required></div>
    <div class="field"><label>Shift start · IST</label><input name="shift_start" type="time" value="09:30" required></div>
    <div class="field"><label>Shift end · IST</label><input name="shift_end" type="time" value="18:30" required></div>
  </div>` }, async (values) => {
    await api("/employees", { method: "POST", body: JSON.stringify(values) });
    closeDialog(); notify("Employee profile created."); await renderView(state.view);
  });
}
function openPunchDialog() {
  const employees = state.employees;
  const options = employees.map((employee) => `<option value="${escapeHTML(employee.emp_code)}">${escapeHTML(employee.name)} · ${escapeHTML(employee.emp_code)}</option>`).join("");
  dialogForm({ title: "Record punch-in", description: "Leave the time blank to use the server's current time.", submit: "Record punch-in", fields: `<div class="form-grid">
    <div class="field field-wide"><label>Employee</label><select name="emp_code" required><option value="">Choose employee</option>${options}</select></div>
    <div class="field"><label>Status</label><select name="status"><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option></select></div>
    <div class="field"><label>Punch time · local</label><input name="punched_at" type="datetime-local"></div>
  </div>` }, async (values) => {
    const body = { emp_code: values.emp_code, status: values.status };
    if (values.punched_at) body.punched_at = new Date(values.punched_at).getTime();
    await api("/attendance/punch-in", { method: "POST", body: JSON.stringify(body) });
    closeDialog(); notify("Punch-in recorded."); await renderView(state.view === "overview" ? "attendance" : state.view);
  });
}
async function punchOut(empCode) {
  if (!window.confirm(`Record punch-out for ${empCode} using the current server time?`)) return;
  try {
    await api("/attendance/punch-out", { method: "POST", body: JSON.stringify({ emp_code: empCode }) });
    notify(`Punch-out recorded for ${empCode}.`); await renderView(state.view);
  } catch (error) { notify(error.message, true); }
}
async function openRegularizeDialog(empCode, attendanceDate) {
  try {
    const result = await api(`/attendance?${queryString({ emp_code: empCode, date_from: attendanceDate, date_to: attendanceDate, page: 1, page_size: 1 })}`);
    const record = result.items[0];
    if (!record) throw new Error("Attendance record no longer exists.");
    const toLocalValue = (milliseconds) => milliseconds == null ? "" : new Date(milliseconds - new Date(milliseconds).getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const statusOptions = ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"].map((status) => `<option ${record.status === status ? "selected" : ""}>${status}</option>`).join("");
    dialogForm({ title: `Correct ${empCode}`, description: `Attendance date ${attendanceDate}. A correction will be added to the audit history.`, submit: "Save correction", fields: `<div class="form-grid">
      <div class="field"><label>Status</label><select name="status">${statusOptions}</select></div>
      <div class="field"><label>Corrected by</label><input name="regularized_by" maxlength="50" required></div>
      <div class="field"><label>Punch-in · local</label><input name="punch_in" type="datetime-local" value="${toLocalValue(record.punch_in)}"></div>
      <div class="field"><label>Punch-out · local</label><input name="punch_out" type="datetime-local" value="${toLocalValue(record.punch_out)}"></div>
      <div class="field field-wide"><label>Reason</label><input name="reason" minlength="5" maxlength="200" required></div>
      <p class="field-wide result-placeholder">For ABSENT or LEAVE, both punch times are cleared automatically.</p>
    </div>` }, async (values) => {
      const body = { status: values.status, regularized_by: values.regularized_by, reason: values.reason };
      if (values.punch_in) body.punch_in = new Date(values.punch_in).getTime();
      if (values.punch_out) body.punch_out = new Date(values.punch_out).getTime();
      await api(`/attendance/${encodeURIComponent(empCode)}/${encodeURIComponent(attendanceDate)}`, { method: "PATCH", body: JSON.stringify(body) });
      closeDialog(); notify("Correction saved to the audit history."); await renderView(state.view);
    });
  } catch (error) { notify(error.message, true); }
}

document.addEventListener("click", (event) => {
  const navigate = event.target.closest("[data-view], [data-navigate]");
  if (navigate) { event.preventDefault(); renderView(navigate.dataset.view || navigate.dataset.navigate); return; }
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "new-employee") openEmployeeDialog();
  else if (action === "new-punch") openPunchDialog();
  else if (action === "close-dialog") closeDialog();
  else if (action === "punch-out") punchOut(button.dataset.employee);
  else if (action === "regularize") openRegularizeDialog(button.dataset.employee, button.dataset.date);
  else if (action === "retry") renderView(state.view);
  else if (action === "employee-prev") { state.employeePage = Math.max(1, state.employeePage - 1); loadEmployees().catch(showPageError); }
  else if (action === "employee-next") { state.employeePage += 1; loadEmployees().catch(showPageError); }
  else if (action === "attendance-prev") { state.attendancePage = Math.max(1, state.attendancePage - 1); loadAttendance().catch(showPageError); }
  else if (action === "attendance-next") { state.attendancePage += 1; loadAttendance().catch(showPageError); }
  else if (action === "clear-attendance-filter") { state.attendanceFilters = {}; state.attendancePage = 1; loadAttendance().catch(showPageError); }
  else if (action === "clear-employee-filter") { state.employeeDepartment = ""; state.employeePage = 1; loadEmployees().catch(showPageError); }
});

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => renderView(button.dataset.view)));
document.querySelector("#today-label").textContent = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "Asia/Kolkata" }).format(new Date());
api("/health").then(() => setService(true)).catch(() => setService(false));
renderView("overview");
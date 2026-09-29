// ==============================================================================
// PART 1: MANUAL TRIGGER - ON EDIT
// Used when a user manually changes a dropdown for a single row.
// ==============================================================================

function confirmAndSendWebhook(e) {
  const CONFIG = {
    SHEET_NAME: 'JOBS CONTROL',
    WEBHOOK_URL: 'https://kzg-qbo-bot.ngrok-free.app/webhook',
    SECRET_TOKEN: 'your_secret_password_123',
    DEDUPE_SECONDS: 10
  };

  const TRIGGER_MAP = {
    7: {
      keyword: 'READY',
      type: 'pipeline_trigger',
      title: 'Transformation',
      needsValidation: false // General sync validation is false, but custom check applies below
    },
    8: {
      keyword: 'SYNC NOW',
      type: 'sync_trigger',
      title: 'QBO Sync',
      needsValidation: true
    },
    9: {
      keyword: 'RECONCILE NOW',
      type: 'reconcile_trigger',
      title: 'QBO Reconciliation',
      needsValidation: false
    }
  };

  const CHECKS = {
    JV: 13,
    EXP: 14,
    TR: 15,
    NO_COL_CHECK: 20 // Column 20 (Column T)
  };

  if (!e || !e.range) return;

  const sheet = e.range.getSheet();
  const row = e.range.getRow();
  const col = e.range.getColumn();

  if (sheet.getName() !== CONFIG.SHEET_NAME || row < 2) return;
  if (e.range.getNumRows() > 1 || e.range.getNumColumns() > 1) return;

  const newVal = String(e.value || e.range.getValue()).toUpperCase().trim();
  const oldVal = String(e.oldValue || '').toUpperCase().trim();

  if (newVal === oldVal || newVal === 'IDLE' || newVal === '') return;

  const action = TRIGGER_MAP[col];
  if (!action || newVal !== action.keyword) return;

  const cache = CacheService.getScriptCache();
  const lockKey = `LOCK_${sheet.getSheetId()}_${e.range.getA1Notation()}_${newVal}`;

  if (cache.get(lockKey)) return;
  cache.put(lockKey, 'TRUE', CONFIG.DEDUPE_SECONDS);

  // --------------------------------------------------------------------------
  // NEW: Formula Block Check for Transformation (Column 7) with Dynamic Country
  // --------------------------------------------------------------------------
  if (col === 7) {
    const isBlocked = String(sheet.getRange(row, CHECKS.NO_COL_CHECK).getDisplayValue()).toUpperCase().trim() === 'TRUE';
    
    if (isBlocked) {
      // Get the country code dynamically from Column 1
      const countryCode = String(sheet.getRange(row, 1).getDisplayValue()).trim();
      
      SpreadsheetApp.getUi().alert(
        'Formula Blocked',
        `Cannot start Transformation.\n\nPlease check the "No." column in ${countryCode} COY raw file and remove any values that are blocking the formula.`,
        SpreadsheetApp.getUi().ButtonSet.OK
      );
      e.range.setValue('Idle');
      return;
    }
  }

  SpreadsheetApp.getActive().toast(`Validating ${action.title}...`, 'System', 3);

  if (action.needsValidation) {
    const statuses = sheet.getRange(row, CHECKS.JV, 1, 3).getDisplayValues()[0];
    const labels = ['Journals', 'Expenses', 'Transfers'];
    const errors = [];

    statuses.forEach((status, i) => {
      const text = String(status).toUpperCase();

      if (text.includes('ERROR') || text.includes('FAIL')) {
        errors.push(`• ${labels[i]}: ${status}`);
      }
    });

    if (errors.length > 0) {
      SpreadsheetApp.getUi().alert(
        'Validation Failed',
        `Cannot start ${action.title}.\n\nIssues found:\n${errors.join('\n')}\n\nPlease fix these first.`,
        SpreadsheetApp.getUi().ButtonSet.OK
      );

      e.range.setValue('Idle');
      return;
    }
  }

  const rowData = sheet.getRange(row, 1, 1, 5).getDisplayValues()[0];

  const payload = {
    event: action.type,
    spreadsheet_id: SpreadsheetApp.getActiveSpreadsheet().getId(),
    client_name: 'KZO',
    country: rowData[0],
    tab_name: rowData[2],
    month: rowData[3],
    last_month_date: rowData[4],
    row_index: row
  };

  SpreadsheetApp.getActive().toast('Sending request to server...', 'System', 3);

  try {
    const response = UrlFetchApp.fetch(CONFIG.WEBHOOK_URL, {
      method: 'post',
      contentType: 'application/json',
      headers: {
        'X-My-Secret-Token': CONFIG.SECRET_TOKEN
      },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });

    const code = response.getResponseCode();

    if (code >= 200 && code < 300) {
      SpreadsheetApp.getActive().toast(`${action.title} started.`, 'Success', 5);
    } else {
      SpreadsheetApp.getUi().alert(
        'Server Error',
        `Code: ${code}\n\n${response.getContentText()}`,
        SpreadsheetApp.getUi().ButtonSet.OK
      );

      e.range.setValue('ERROR');
    }
  } catch (err) {
    SpreadsheetApp.getUi().alert(
      'Connection Failed',
      `Could not reach the server.\n\nError: ${err}`,
      SpreadsheetApp.getUi().ButtonSet.OK
    );

    e.range.setValue('ERROR');
  }
}


// ==============================================================================
// PART 2: BULK UI ORCHESTRATOR
// Used when a user clicks the custom menu to process many rows.
// ==============================================================================

const BULK_CONFIG = {
  SHEET_NAME: 'JOBS CONTROL',
  MENU_NAME: 'QBO Automation',
  CLIENT_NAME: 'KZO',
  WEBHOOK_URL: 'https://kzg-qbo-bot.ngrok-free.app/webhook',
  SECRET_TOKEN: 'your_secret_password_123'
};

const BULK_ACTIONS = {
  transform: {
    col: 7,
    label: 'Transformation',
    keyword: 'READY',
    eventName: 'pipeline_trigger'
  },
  sync: {
    col: 8,
    label: 'QBO Sync',
    keyword: 'SYNC NOW',
    eventName: 'sync_trigger'
  },
  reconcile: {
    col: 9,
    label: 'QBO Reconciliation',
    keyword: 'RECONCILE NOW',
    eventName: 'reconcile_trigger'
  }
};

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu(BULK_CONFIG.MENU_NAME)
    .addItem('Transform', 'openTransformDialog')
    .addItem('Sync', 'openSyncDialog')
    .addItem('Reconcile', 'openReconcileDialog')
    .addToUi();
}

function openTransformDialog() {
  openOrchestratorDialog_('transform');
}

function openSyncDialog() {
  openOrchestratorDialog_('sync');
}

function openReconcileDialog() {
  openOrchestratorDialog_('reconcile');
}

function openOrchestratorDialog_(actionKey) {
  const dialogData = getDialogData_(actionKey);

  const html = HtmlService
    .createHtmlOutput(buildQueueDialogHtml_(dialogData))
    .setWidth(520)
    .setHeight(640);

  SpreadsheetApp.getUi().showModalDialog(
    html,
    `Bulk ${dialogData.actionLabel}`
  );
}

function getDialogData_(actionKey) {
  const sheet = SpreadsheetApp
    .getActiveSpreadsheet()
    .getSheetByName(BULK_CONFIG.SHEET_NAME);

  if (!sheet) {
    throw new Error(`Sheet "${BULK_CONFIG.SHEET_NAME}" not found.`);
  }

  const values = sheet.getDataRange().getDisplayValues();

  if (values.length < 2) {
    throw new Error('No data found in the sheet.');
  }

  const rowItems = [];

  for (let i = 1; i < values.length; i++) {
    const country = String(values[i][0]).trim();
    const month = String(values[i][3]).trim();

    if (country && month) {
      rowItems.push({
        rowIndex: i + 1,
        country,
        month
      });
    }
  }

  const months = [...new Set(rowItems.map(item => item.month))]
    .sort()
    .reverse();

  const countriesByMonth = {};

  months.forEach(month => {
    countriesByMonth[month] = [...new Set(
      rowItems
        .filter(item => item.month === month)
        .map(item => item.country)
    )].sort();
  });

  return {
    actionKey,
    actionLabel: BULK_ACTIONS[actionKey].label,
    months,
    countriesByMonth
  };
}

function getTargetsForExecution(actionKey, month, countries) {
  const sheet = SpreadsheetApp
    .getActiveSpreadsheet()
    .getSheetByName(BULK_CONFIG.SHEET_NAME);

  if (!sheet) {
    throw new Error(`Sheet "${BULK_CONFIG.SHEET_NAME}" not found.`);
  }

  const values = sheet.getDataRange().getDisplayValues();
  const targets = [];

  for (let i = 1; i < values.length; i++) {
    const rowCountry = String(values[i][0]).trim();
    const rowMonth = String(values[i][3]).trim();

    if (rowMonth === month && countries.includes(rowCountry)) {
      targets.push({
        rowIndex: i + 1,
        country: rowCountry,
        month: rowMonth
      });
    }
  }

  return targets;
}

function triggerSingleRowAction(actionKey, rowIndex) {
  const sheet = SpreadsheetApp
    .getActiveSpreadsheet()
    .getSheetByName(BULK_CONFIG.SHEET_NAME);

  if (!sheet) {
    throw new Error(`Sheet "${BULK_CONFIG.SHEET_NAME}" not found.`);
  }

  const config = BULK_ACTIONS[actionKey];

  if (!config) {
    throw new Error(`Invalid action key: ${actionKey}`);
  }

// --------------------------------------------------------------------------
  // NEW: Bulk Queue Formula Block Check for Transformation with Dynamic Country
  // --------------------------------------------------------------------------
  if (actionKey === 'transform') {
    const isBlocked = String(sheet.getRange(rowIndex, 20).getDisplayValue()).toUpperCase().trim() === 'TRUE';
    
    if (isBlocked) {
      // Get the country code dynamically from Column 1
      const countryCode = String(sheet.getRange(rowIndex, 1).getDisplayValue()).trim();
      
      sheet.getRange(rowIndex, config.col).setValue('ERROR');
      return {
        success: false,
        message: `Blocked: Clean the "No." column in ${countryCode} COY raw file to clear formula errors.`
      };
    }
  }
  
  const statusCell = sheet.getRange(rowIndex, config.col);
  statusCell.setValue(config.keyword);
  SpreadsheetApp.flush();

  const rowData = sheet.getRange(rowIndex, 1, 1, 5).getDisplayValues()[0];

  const payload = {
    event: config.eventName,
    spreadsheet_id: SpreadsheetApp.getActiveSpreadsheet().getId(),
    client_name: BULK_CONFIG.CLIENT_NAME,
    country: rowData[0],
    tab_name: rowData[2],
    month: rowData[3],
    last_month_date: rowData[4],
    row_index: rowIndex
  };

  try {
    const response = UrlFetchApp.fetch(BULK_CONFIG.WEBHOOK_URL, {
      method: 'post',
      contentType: 'application/json',
      headers: {
        'X-My-Secret-Token': BULK_CONFIG.SECRET_TOKEN
      },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });

    const code = response.getResponseCode();

    if (code >= 200 && code < 300) {
      return {
        success: true,
        code
      };
    }

    statusCell.setValue('ERROR');

    return {
      success: false,
      code,
      message: response.getContentText()
    };
  } catch (err) {
    statusCell.setValue('ERROR');

    return {
      success: false,
      message: String(err)
    };
  }
}

function checkRowStatus(actionKey, rowIndex) {
  const sheet = SpreadsheetApp
    .getActiveSpreadsheet()
    .getSheetByName(BULK_CONFIG.SHEET_NAME);

  if (!sheet) {
    throw new Error(`Sheet "${BULK_CONFIG.SHEET_NAME}" not found.`);
  }

  const config = BULK_ACTIONS[actionKey];

  if (!config) {
    throw new Error(`Invalid action key: ${actionKey}`);
  }

  return String(sheet.getRange(rowIndex, config.col).getDisplayValue())
    .toUpperCase()
    .trim();
}


// ==============================================================================
// PART 3: CLEAN MODERN BULK DIALOG HTML
// ==============================================================================

function buildQueueDialogHtml_(data) {
  const safeData = JSON.stringify(data).replace(/</g, '\\u003c');

  return `
<!DOCTYPE html>
<html>
<head>
  <base target="_top">

  <style>
    :root {
      --primary: #1a73e8;
      --primary-dark: #1557b0;
      --text: #202124;
      --muted: #5f6368;
      --border: #dadce0;
      --bg: #ffffff;
      --soft-bg: #f8fafd;
      --soft-gray: #f1f3f4;
      --success-bg: #e6f4ea;
      --success-text: #137333;
      --error-bg: #fce8e6;
      --error-text: #c5221f;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      padding: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Google Sans", Roboto, Arial, sans-serif;
      font-size: 14px;
    }

    .app {
      padding: 22px;
    }

    .header {
      margin-bottom: 18px;
    }

    .title {
      margin: 0;
      font-size: 20px;
      font-weight: 600;
      color: var(--text);
    }

    .subtitle {
      margin: 8px 0 0;
      color: var(--muted);
      line-height: 1.45;
    }

    .card {
      border: 1px solid var(--border);
      border-radius: 12px;
      background: #fff;
      padding: 16px;
      margin-bottom: 16px;
    }

    .field {
      margin-bottom: 16px;
    }

    .field:last-child {
      margin-bottom: 0;
    }

    label.field-label {
      display: block;
      margin-bottom: 8px;
      font-weight: 600;
      color: var(--text);
    }

    select {
      width: 100%;
      padding: 11px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      color: var(--text);
      font-size: 14px;
      outline: none;
    }

    select:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 2px rgba(26, 115, 232, 0.12);
    }

    .toolbar {
      display: flex;
      gap: 8px;
      margin-bottom: 10px;
    }

    button {
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      cursor: pointer;
      font-size: 14px;
      font-weight: 600;
      font-family: inherit;
    }

    button:disabled {
      opacity: 0.65;
      cursor: not-allowed;
    }

    .btn-primary {
      width: 100%;
      color: #fff;
      background: var(--primary);
    }

    .btn-primary:hover:not(:disabled) {
      background: var(--primary-dark);
    }

    .btn-secondary {
      color: #3c4043;
      background: var(--soft-gray);
      border: 1px solid var(--border);
    }

    .btn-secondary:hover:not(:disabled) {
      background: #e8eaed;
    }

    .country-box {
      max-height: 250px;
      overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--soft-bg);
      padding: 8px;
    }

    .country-item {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px;
      border-radius: 8px;
      cursor: pointer;
      user-select: none;
    }

    .country-item:hover {
      background: #eef3fb;
    }

    .country-item input {
      width: 16px;
      height: 16px;
      cursor: pointer;
    }

    .status-card {
      display: none;
    }

    .status-banner {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 14px;
      border-radius: 12px;
      background: #e8f0fe;
      color: var(--primary);
      margin-bottom: 14px;
      font-weight: 600;
      line-height: 1.4;
    }

    .status-banner.success {
      background: var(--success-bg);
      color: var(--success-text);
    }

    .status-banner.error {
      background: var(--error-bg);
      color: var(--error-text);
    }

    .spinner {
      width: 18px;
      height: 18px;
      border: 3px solid rgba(0, 0, 0, 0.12);
      border-left-color: currentColor;
      border-radius: 50%;
      animation: spin 0.9s linear infinite;
      flex: 0 0 auto;
    }

    @keyframes spin {
      to {
        transform: rotate(360deg);
      }
    }

    .progress-wrap {
      margin-bottom: 16px;
    }

    .progress-meta {
      display: flex;
      justify-content: space-between;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 13px;
    }

    .progress-track {
      width: 100%;
      height: 9px;
      background: #e8eaed;
      border-radius: 99px;
      overflow: hidden;
    }

    .progress-fill {
      height: 100%;
      width: 0%;
      background: var(--primary);
      border-radius: 99px;
      transition: width 0.35s ease;
    }

    .queue-list {
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
      max-height: 330px;
      overflow-y: auto;
      margin-bottom: 16px;
    }

    .queue-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid #f1f3f4;
      background: #fff;
    }

    .queue-item:last-child {
      border-bottom: 0;
    }

    .queue-item.active {
      background: #f3f7ff;
      box-shadow: inset 4px 0 0 var(--primary);
    }

    .queue-title {
      font-weight: 600;
      color: var(--text);
      margin-bottom: 3px;
    }

    .queue-subtitle {
      color: var(--muted);
      font-size: 13px;
    }

    .badge {
      flex: 0 0 auto;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.4px;
    }

    .badge.pending {
      background: var(--soft-gray);
      color: var(--muted);
    }

    .badge.running {
      background: #fef7e0;
      color: #b06000;
    }

    .badge.done {
      background: var(--success-bg);
      color: var(--success-text);
    }

    .badge.error {
      background: var(--error-bg);
      color: var(--error-text);
    }

    .footer-actions {
      display: flex;
      gap: 10px;
    }

    .footer-actions button {
      flex: 1;
    }

    .small-help {
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      text-align: center;
    }
  </style>
</head>

<body>
  <div class="app">

    <div id="setupView">
      <div class="header">
        <h1 class="title">Select jobs to run</h1>
        <p class="subtitle">
          Choose one month and one or more countries.
        </p>
      </div>

      <div class="card">
        <div class="field">
          <label class="field-label" for="monthSelect">Target Month</label>
          <select id="monthSelect"></select>
        </div>

        <div class="field">
          <label class="field-label">Target Countries</label>

          <div class="toolbar">
            <button class="btn-secondary" onclick="toggleAllCountries(true)">Select All</button>
            <button class="btn-secondary" onclick="toggleAllCountries(false)">Clear</button>
          </div>

          <div id="countryBox" class="country-box"></div>
        </div>
      </div>

      <button id="runBtn" class="btn-primary" onclick="startExecution()">
        Start Run
      </button>

      <div class="small-help">
        You can close this window with X anytime.
      </div>
    </div>


    <div id="statusView" class="status-card">
      <div class="header">
        <h1 class="title">Running jobs</h1>
        <p class="subtitle">
          Progress updates will appear below.
        </p>
      </div>

      <div id="statusBanner" class="status-banner">
        <div id="spinner" class="spinner"></div>
        <div id="statusMessage">Preparing queue...</div>
      </div>

      <div class="progress-wrap">
        <div class="progress-meta">
          <span id="progressText">0 of 0 processed</span>
          <span id="progressPercent">0%</span>
        </div>

        <div class="progress-track">
          <div id="progressBar" class="progress-fill"></div>
        </div>
      </div>

      <div id="queueList" class="queue-list"></div>

      <div class="footer-actions">
        <button class="btn-secondary" onclick="closeWindow()">
          Close Window
        </button>
      </div>

      <div class="small-help">
        You can also close this window with X.
      </div>
    </div>

  </div>


  <script>
    const DATA = ${safeData};

    let currentQueue = [];
    let currentIndex = 0;
    let pollTimer = null;
    let isFinished = false;

    const POLL_EVERY_MS = 5000;

    init();

    function init() {
      renderMonths();
      renderCountries();

      document
        .getElementById('monthSelect')
        .addEventListener('change', renderCountries);
    }

    function renderMonths() {
      const monthSelect = document.getElementById('monthSelect');

      if (!DATA.months || DATA.months.length === 0) {
        monthSelect.innerHTML = '<option value="">No months found</option>';
        document.getElementById('runBtn').disabled = true;
        return;
      }

      monthSelect.innerHTML = DATA.months
        .map(month => '<option value="' + escapeHtml(month) + '">' + escapeHtml(month) + '</option>')
        .join('');
    }

    function renderCountries() {
      const selectedMonth = document.getElementById('monthSelect').value;
      const countries = DATA.countriesByMonth[selectedMonth] || [];
      const countryBox = document.getElementById('countryBox');

      if (!countries.length) {
        countryBox.innerHTML = '<div class="queue-subtitle">No countries found for this month.</div>';
        return;
      }

      countryBox.innerHTML = countries.map(country => {
        const safeCountry = escapeHtml(country);

        return (
          '<label class="country-item">' +
            '<input type="checkbox" name="country" value="' + safeCountry + '">' +
            '<span>' + safeCountry + '</span>' +
          '</label>'
        );
      }).join('');
    }

    function toggleAllCountries(checked) {
      document
        .querySelectorAll('input[name="country"]')
        .forEach(cb => cb.checked = checked);
    }

    function startExecution() {
      const month = document.getElementById('monthSelect').value;

      const countries = Array
        .from(document.querySelectorAll('input[name="country"]:checked'))
        .map(cb => cb.value);

      if (!month) {
        alert('Please select a month.');
        return;
      }

      if (!countries.length) {
        alert('Please select at least one country.');
        return;
      }

      const runBtn = document.getElementById('runBtn');
      runBtn.disabled = true;
      runBtn.textContent = 'Preparing...';

      google.script.run
        .withSuccessHandler(initQueue)
        .withFailureHandler(err => {
          runBtn.disabled = false;
          runBtn.textContent = 'Start Run';
          alert(formatError(err));
        })
        .getTargetsForExecution(DATA.actionKey, month, countries);
    }

    function initQueue(targets) {
      if (!targets || !targets.length) {
        const runBtn = document.getElementById('runBtn');
        runBtn.disabled = false;
        runBtn.textContent = 'Start Run';

        alert('No matching rows were found.');
        return;
      }

      currentQueue = targets;
      currentIndex = 0;
      isFinished = false;

      document.getElementById('setupView').style.display = 'none';
      document.getElementById('statusView').style.display = 'block';

      updateQueueUI();
      processCurrentRow();
    }

    function processCurrentRow() {
      if (isFinished) return;

      updateQueueUI();

      if (currentIndex >= currentQueue.length) {
        finishRun('Done. All selected rows have been processed.', 'success');
        return;
      }

      const target = currentQueue[currentIndex];

      setStatusMessage(
        'Running row ' + target.rowIndex + ' • ' + target.country,
        false
      );

      google.script.run
        .withSuccessHandler(res => {
          if (isFinished) return;

          if (res && res.success) {
            startPollingCurrentRow();
          } else {
            markCurrentRowAsError();
            moveToNextRow();
          }
        })
        .withFailureHandler(() => {
          if (isFinished) return;

          markCurrentRowAsError();
          moveToNextRow();
        })
        .triggerSingleRowAction(DATA.actionKey, target.rowIndex);
    }

    function startPollingCurrentRow() {
      clearPollTimer();

      pollTimer = setInterval(() => {
        pollCurrentRow();
      }, POLL_EVERY_MS);

      pollCurrentRow();
    }

    function pollCurrentRow() {
      if (isFinished) {
        clearPollTimer();
        return;
      }

      const target = currentQueue[currentIndex];

      if (!target) {
        clearPollTimer();
        finishRun('Done. All selected rows have been processed.', 'success');
        return;
      }

      google.script.run
        .withSuccessHandler(statusText => {
          if (isFinished) return;

          const status = String(statusText || '').toUpperCase().trim();

          if (status === 'DONE') {
            clearPollTimer();
            markCurrentRowAsDone();
            moveToNextRow();
            return;
          }

          if (
            status.includes('ERROR') ||
            status.includes('FAIL') ||
            status.includes('FAILED')
          ) {
            clearPollTimer();
            markCurrentRowAsError();
            moveToNextRow();
          }
        })
        .withFailureHandler(() => {
          if (isFinished) return;

          clearPollTimer();
          markCurrentRowAsError();
          moveToNextRow();
        })
        .checkRowStatus(DATA.actionKey, target.rowIndex);
    }

    function moveToNextRow() {
      currentIndex++;
      updateQueueUI();
      processCurrentRow();
    }

    function markCurrentRowAsDone() {
      updateBadge(currentIndex, 'done', 'Done');
    }

    function markCurrentRowAsError() {
      updateBadge(currentIndex, 'error', 'Error');
    }

    function updateQueueUI() {
      const total = currentQueue.length;
      const processed = Math.min(currentIndex, total);
      const percent = total ? Math.round((processed / total) * 100) : 0;

      document.getElementById('progressText').textContent =
        processed + ' of ' + total + ' processed';

      document.getElementById('progressPercent').textContent = percent + '%';
      document.getElementById('progressBar').style.width = percent + '%';

      const html = currentQueue.map((item, index) => {
        let badgeClass = 'pending';
        let badgeText = 'Pending';
        let activeClass = '';

        if (index < currentIndex) {
          badgeClass = 'done';
          badgeText = 'Done';
        } else if (index === currentIndex && !isFinished) {
          badgeClass = 'running';
          badgeText = 'Running';
          activeClass = 'active';
        }

        return (
          '<div class="queue-item ' + activeClass + '" id="queueItem-' + index + '">' +
            '<div>' +
              '<div class="queue-title">Row ' + escapeHtml(String(item.rowIndex)) + '</div>' +
              '<div class="queue-subtitle">' + escapeHtml(item.country) + '</div>' +
            '</div>' +
            '<span class="badge ' + badgeClass + '" id="badge-' + index + '">' + badgeText + '</span>' +
          '</div>'
        );
      }).join('');

      document.getElementById('queueList').innerHTML = html;

      scrollActiveRowIntoView();
    }

    function updateBadge(index, badgeClass, badgeText) {
      const badge = document.getElementById('badge-' + index);

      if (!badge) return;

      badge.className = 'badge ' + badgeClass;
      badge.textContent = badgeText;
    }

    function scrollActiveRowIntoView() {
      const activeRow = document.querySelector('.queue-item.active');

      if (activeRow && activeRow.scrollIntoView) {
        activeRow.scrollIntoView({
          block: 'nearest'
        });
      }
    }

    function finishRun(message, type) {
      isFinished = true;
      clearPollTimer();

      const total = currentQueue.length;
      currentIndex = total;

      updateQueueUI();

      const banner = document.getElementById('statusBanner');
      banner.className = 'status-banner ' + type;

      document.getElementById('spinner').style.display = 'none';
      document.getElementById('statusMessage').textContent = message;

      document.getElementById('progressText').textContent =
        total + ' of ' + total + ' processed';

      document.getElementById('progressPercent').textContent = '100%';
      document.getElementById('progressBar').style.width = '100%';
    }

    function setStatusMessage(message, hideSpinner) {
      document.getElementById('statusMessage').textContent = message;
      document.getElementById('spinner').style.display = hideSpinner ? 'none' : 'block';
    }

    function clearPollTimer() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function closeWindow() {
      clearPollTimer();
      google.script.host.close();
    }

    function formatError(err) {
      if (!err) return 'Unknown error.';
      if (typeof err === 'string') return err;
      if (err.message) return err.message;
      return JSON.stringify(err);
    }

    function escapeHtml(value) {
      return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }
  </script>
</body>
</html>
  `;
}
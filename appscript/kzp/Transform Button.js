function confirmAndSendWebhook(e) {
  // ==========================================
  // 1. CONFIGURATION & SETUP
  // ==========================================
  const CONFIG = {
    SHEET_NAME: "JOBS CONTROL", 
    WEBHOOK_URL: "https://kzg-qbo-bot.ngrok-free.app/webhook", 
    SECRET_TOKEN: "your_secret_password_123",
    DEDUPE_SECONDS: 10
  };

  const TRIGGER_MAP = {
    7:  { // Column G - Transform (Dropdown)
      keyword: "READY",
      type: "pipeline_trigger",
      title: "TRANSFORM Data",
      script: "run_ingestion.py",
      needsValidation: false
    },
    8:  { // Column H - QBO Sync (Dropdown)
      keyword: "SYNC NOW",
      type: "sync_trigger",
      title: "SYNC to QuickBooks",
      script: "run_syncing.py",
      needsValidation: true 
    },
    9:  { // Column I - QBO Reconcile (Dropdown)
      keyword: "RECONCILE NOW",
      type: "reconcile_trigger",
      title: "RECONCILE QBO Data",
      script: "run_reconciliation.py",
      needsValidation: false
    }
  };

  // Validation Columns for Sync
  // M=13 (Journal), N=14 (Expense), O=15 (Transfer)
  const CHECKS = { JV: 13, EXP: 14, TR: 15 }; 

  // ==========================================
  // 2. BASIC CHECKS (Fail Fast)
  // ==========================================
  if (!e || !e.range) return;
  
  const sheet = e.range.getSheet();
  if (sheet.getName() !== CONFIG.SHEET_NAME || e.range.getRow() < 2) return;
  if (e.range.getNumRows() > 1 || e.range.getNumColumns() > 1) return; 

  const newVal = String(e.value || e.range.getValue()).toUpperCase().trim();
  const oldVal = String(e.oldValue || "").toUpperCase().trim();

  if (newVal === oldVal || newVal === "IDLE" || newVal === "") return;

  const colIndex = e.range.getColumn();
  const action = TRIGGER_MAP[colIndex];

  if (!action) return; 
  if (newVal !== action.keyword) return; 

  // ==========================================
  // 3. LOCK & DEBOUNCE (Prevent Double Clicks)
  // ==========================================
  const cache = CacheService.getScriptCache();
  const lockKey = `LOCK_${sheet.getSheetId()}_${e.range.getA1Notation()}_${newVal}`;
  
  if (cache.get(lockKey)) return; 
  cache.put(lockKey, "TRUE", CONFIG.DEDUPE_SECONDS);

  SpreadsheetApp.getActive().toast(`🔍 Validating ${action.title}...`, "System");

  // ==========================================
  // 4. VALIDATION LOGIC
  // ==========================================
  if (action.needsValidation) {
    const statuses = sheet.getRange(e.range.getRow(), CHECKS.JV, 1, 3).getDisplayValues()[0];
    const errors = [];
    const labels = ["Journals", "Expenses", "Transfers"];

    statuses.forEach((status, i) => {
      const s = String(status).toUpperCase();
      if (s.includes("ERROR") || s.includes("FAIL")) {
        errors.push(`• ${labels[i]}: ${status}`);
      }
    });

    if (errors.length > 0) {
      const ui = SpreadsheetApp.getUi();
      ui.alert(
        "🚫 Validation Failed", 
        `Cannot start ${action.title}.\n\nIssues found:\n${errors.join("\n")}\n\nPlease fix errors in the check columns first.`, 
        ui.ButtonSet.OK
      );
      e.range.setValue("IDLE"); // Reset status
      return;
    }
  }

  // ==========================================
  // 5. USER CONFIRMATION
  // ==========================================
  const ui = SpreadsheetApp.getUi();

  // Expanded to grab columns 1 to 5 (A through E)
  const rowData = sheet.getRange(e.range.getRow(), 1, 1, 5).getDisplayValues()[0];
  
  const country = rowData[0]; // Col A
  const tabName = rowData[2]; // Col C
  const monthStr = rowData[3]; // Col D (Month)
  const lastMonthDateStr = rowData[4]; // Col E (Last Month Date)

  const confirm = ui.alert(
    `🚀 Confirm Start`,
    `Do you want to run: ${action.title}?\n\n` +
    `📍 Client: ${country}\n` +
    `📅 Month: ${monthStr}\n` +
    `⏮️ Last Month Date: ${lastMonthDateStr}`, 
    ui.ButtonSet.YES_NO
  );

  if (confirm !== ui.Button.YES) {
    e.range.setValue("IDLE");
    SpreadsheetApp.getActive().toast("❌ Action cancelled.", "System");
    return;
  }

  // ==========================================
  // 6. SEND WEBHOOK
  // ==========================================
  SpreadsheetApp.getActive().toast("⏳ Sending request to server...", "System");

  const payload = {
    event: action.type,

    // Required routing fields:
    spreadsheet_id: SpreadsheetApp.getActiveSpreadsheet().getId(), // queue key + fallback target
    client_name: "KZP", // or KZP / KZDW / S5 for that control file

    // Keep business data:
    country: country,
    tab_name: tabName,
    month: monthStr,
    last_month_date: lastMonthDateStr,
    row_index: e.range.getRow()
  };

  try {
    const options = {
      method: "post",
      contentType: "application/json",
      headers: { "X-My-Secret-Token": CONFIG.SECRET_TOKEN },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    };

    const response = UrlFetchApp.fetch(CONFIG.WEBHOOK_URL, options);
    const code = response.getResponseCode();

    if (code === 200) {
      SpreadsheetApp.getActive().toast(`✅ ${action.title} Started!`, "Success", 5);
    } else {
      ui.alert("⚠️ Server Error", `Response Code: ${code}\n\n${response.getContentText()}`, ui.ButtonSet.OK);
      e.range.setValue("IDLE");
    }

  } catch (err) {
    ui.alert("❌ Connection Failed", `Could not reach server.\n\nError: ${err}`, ui.ButtonSet.OK);
    e.range.setValue("IDLE");
  }
}
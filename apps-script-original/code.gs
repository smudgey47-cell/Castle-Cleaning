const CLEANING_CONFIG = {
  spreadsheetName: 'Castle Sports Lounge - Cleaning Schedule Monitor',
  sheets: {
    settings: 'Settings',
    logs: 'Cleaning Logs',
    templates: 'Check Templates'
  },
  defaults: {
    venueName: 'Castle Sports Lounge',
    openTempDueBy: '10:00',
    closeTempDueBy: '23:30',
    openCloseMinTemp: 0,
    openCloseMaxTemp: 8,
    toiletOpenDueBy: '10:00',
    toiletTwoPmDueBy: '14:00',
    toiletSixPmDueBy: '18:00',
    toiletEightPmDueBy: '20:00',
    toiletCloseDueBy: '23:30',
    rotaSpreadsheetId: '',
    rotaSheetName: 'Shifts'
  }
};

function doGet() {
  ensureCleaningSetup_();
  return HtmlService.createTemplateFromFile('Index')
    .evaluate()
    .setTitle('Castle Cleaning Schedule')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

function setupCleaningApp() {
  const ss = ensureCleaningSetup_();
  return {
    ok: true,
    spreadsheetUrl: ss.getUrl(),
    message: 'Cleaning schedule app is ready.'
  };
}

function setupCleaningAppSafe() {
  try {
    return {
      ok: true,
      data: setupCleaningApp()
    };
  } catch (error) {
    return safeError_(error);
  }
}

function testCleaningApp() {
  const data = getCleaningAppData();
  return {
    ok: true,
    taskCount: data.tasks.length,
    firstTask: data.tasks[0] || null,
    spreadsheetUrl: SpreadsheetApp.openById(PropertiesService.getScriptProperties().getProperty('CLEANING_SPREADSHEET_ID')).getUrl()
  };
}

function getCleaningAppData() {
  const ss = ensureCleaningSetup_();
  const settings = getSettings_(ss);
  const logs = getLogsForDate_(ss, todayKey_());
  const tasks = buildTasksForDate_(settings, logs, new Date(), ss);

  return {
    venueName: settings.venueName,
    today: todayDisplay_(),
    settings,
    tasks,
    stats: buildStats_(tasks, logs),
    recentLogs: getRecentLogs_(ss, 50)
  };
}

function getCleaningBootstrapData() {
  const data = getCleaningAppData();
  data.staffOptions = getCleaningStaffOptions();
  return data;
}

function getCleaningBootstrapDataSafe() {
  try {
    return {
      ok: true,
      data: getCleaningBootstrapData()
    };
  } catch (error) {
    return safeError_(error);
  }
}

function getCleaningBootstrapDataJson() {
  return JSON.stringify(getCleaningBootstrapDataSafe());
}

function getCleaningAppDataSafe() {
  try {
    return {
      ok: true,
      data: getCleaningAppData()
    };
  } catch (error) {
    return safeError_(error);
  }
}

function getCleaningStaffOptions() {
  const ss = ensureCleaningSetup_();
  const settings = getSettings_(ss);
  return getTodaysRotaStaff_(settings);
}

function getCleaningStaffOptionsSafe() {
  try {
    return {
      ok: true,
      data: getCleaningStaffOptions()
    };
  } catch (error) {
    return safeError_(error);
  }
}

function saveCleaningCheck(payload) {
  if (!payload || !payload.taskId || !payload.staffName) {
    throw new Error('Staff name and task are required.');
  }

  const ss = ensureCleaningSetup_();
  const settings = getSettings_(ss);
  const task = buildTasksForDate_(settings, getLogsForDate_(ss, todayKey_()), new Date(), ss)
    .find(item => item.id === payload.taskId);

  if (!task) {
    throw new Error('This check could not be found. Refresh and try again.');
  }

  const value = normaliseValue_(payload.value);
  const status = getSubmittedStatus_(task, value, settings);
  const logSheet = ss.getSheetByName(CLEANING_CONFIG.sheets.logs);

  logSheet.appendRow([
    new Date(),
    todayKey_(),
    task.type,
    task.area,
    task.slotLabel,
    payload.staffName.trim(),
    value,
    status,
    payload.notes ? String(payload.notes).trim() : '',
    task.id
  ]);

  return getCleaningAppData();
}

function saveCleaningCheckSafe(payload) {
  try {
    return {
      ok: true,
      data: saveCleaningCheck(payload)
    };
  } catch (error) {
    return safeError_(error);
  }
}

function saveCleaningSettings(settings) {
  const ss = ensureCleaningSetup_();
  const sheet = ss.getSheetByName(CLEANING_CONFIG.sheets.settings);
  const current = getSettings_(ss);
  const next = Object.assign({}, current, settings || {});
  const rows = Object.keys(next).map(key => [key, next[key]]);

  sheet.clear();
  sheet.getRange(1, 1, 1, 2).setValues([['Setting', 'Value']]);
  sheet.getRange(2, 1, rows.length, 2).setValues(rows);
  sheet.autoResizeColumns(1, 2);

  return getCleaningAppData();
}

function saveCleaningSettingsSafe(settings) {
  try {
    return {
      ok: true,
      data: saveCleaningSettings(settings)
    };
  } catch (error) {
    return safeError_(error);
  }
}

function testRotaLink() {
  const ss = ensureCleaningSetup_();
  const settings = getSettings_(ss);
  return {
    ok: true,
    staffOptions: getTodaysRotaStaff_(settings)
  };
}

function getCleaningLogCsv() {
  const ss = ensureCleaningSetup_();
  const sheet = ss.getSheetByName(CLEANING_CONFIG.sheets.logs);
  const values = sheet.getDataRange().getValues();
  return values.map(row => row.map(csvCell_).join(',')).join('\n');
}

function getCleaningLogCsvSafe() {
  try {
    return {
      ok: true,
      data: getCleaningLogCsv()
    };
  } catch (error) {
    return safeError_(error);
  }
}

function safeError_(error) {
  return {
    ok: false,
    error: error && error.message ? error.message : String(error),
    stack: error && error.stack ? String(error.stack) : ''
  };
}

function include(filename) {
  return HtmlService.createHtmlOutputFromFile(filename).getContent();
}

function ensureCleaningSetup_() {
  const props = PropertiesService.getScriptProperties();
  let spreadsheetId = props.getProperty('CLEANING_SPREADSHEET_ID');
  let ss = null;

  if (spreadsheetId) {
    try {
      ss = SpreadsheetApp.openById(spreadsheetId);
    } catch (error) {
      props.deleteProperty('CLEANING_SPREADSHEET_ID');
      spreadsheetId = null;
    }
  }

  if (!ss) {
    ss = SpreadsheetApp.create(CLEANING_CONFIG.spreadsheetName);
    props.setProperty('CLEANING_SPREADSHEET_ID', ss.getId());
  }

  ensureSheet_(ss, CLEANING_CONFIG.sheets.settings, [['Setting', 'Value']], getDefaultSettingsRows_());
  ensureSheet_(ss, CLEANING_CONFIG.sheets.logs, [[
    'Timestamp',
    'Date',
    'Task Type',
    'Area',
    'Slot',
    'Staff Name',
    'Value',
    'Status',
    'Notes',
    'Task ID'
  ]]);
  ensureSheet_(ss, CLEANING_CONFIG.sheets.templates, [['Task Type', 'Area', 'Schedule', 'Input']], getDefaultTemplateRows_());

  ensureSettingsDefaults_(ss);
  ensureTemplateDefaults_(ss);
  formatSheets_(ss);
  return ss;
}

function ensureSheet_(ss, name, headerRows, defaultRows) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
  }

  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, headerRows.length, headerRows[0].length).setValues(headerRows);
    if (defaultRows && defaultRows.length) {
      sheet.getRange(headerRows.length + 1, 1, defaultRows.length, defaultRows[0].length).setValues(defaultRows);
    }
  }

  return sheet;
}

function formatSheets_(ss) {
  Object.keys(CLEANING_CONFIG.sheets).forEach(key => {
    const sheet = ss.getSheetByName(CLEANING_CONFIG.sheets[key]);
    if (!sheet) return;
    const lastColumn = Math.max(sheet.getLastColumn(), 1);
    sheet.getRange(1, 1, 1, lastColumn).setFontWeight('bold').setBackground('#10251d').setFontColor('#ffffff');
    sheet.setFrozenRows(1);
    sheet.autoResizeColumns(1, lastColumn);
  });
}

function getDefaultSettingsRows_() {
  return Object.keys(CLEANING_CONFIG.defaults).map(key => [key, CLEANING_CONFIG.defaults[key]]);
}

function getDefaultTemplateRows_() {
  return [
    ['Open/Close Temp', 'Bar', 'Open and close', 'Temperature'],
    ['Open/Close Temp', 'Cellar', 'Open and close', 'Temperature'],
    ['Open/Close Temp', 'Canteen', 'Open and close', 'Temperature'],
    ['Toilet Check', 'Ladies Toilets', 'Open, 2pm, 6pm, 8pm and close', 'Checklist'],
    ['Toilet Check', 'Gents Toilets', 'Open, 2pm, 6pm, 8pm and close', 'Checklist'],
    ['Toilet Check', 'Accessible Toilet', 'Open, 2pm, 6pm, 8pm and close', 'Checklist']
  ];
}

function ensureSettingsDefaults_(ss) {
  const sheet = ss.getSheetByName(CLEANING_CONFIG.sheets.settings);
  const values = sheet.getDataRange().getValues();
  const existing = {};

  for (let i = 1; i < values.length; i++) {
    existing[String(values[i][0])] = true;
  }

  const missing = getDefaultSettingsRows_().filter(row => !existing[String(row[0])]);
  if (missing.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, missing.length, 2).setValues(missing);
  }
}

function ensureTemplateDefaults_(ss) {
  const sheet = ss.getSheetByName(CLEANING_CONFIG.sheets.templates);
  const values = sheet.getDataRange().getValues();
  const existing = {};

  for (let i = 1; i < values.length; i++) {
    existing[[values[i][0], values[i][1]].join('|')] = true;
  }

  const missing = getDefaultTemplateRows_().filter(row => !existing[[row[0], row[1]].join('|')]);
  if (missing.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, missing.length, 4).setValues(missing);
  }
}

function getSettings_(ss) {
  const sheet = ss.getSheetByName(CLEANING_CONFIG.sheets.settings);
  const values = sheet.getDataRange().getDisplayValues();
  const settings = Object.assign({}, CLEANING_CONFIG.defaults);

  for (let i = 1; i < values.length; i++) {
    if (values[i][0] !== '') settings[values[i][0]] = values[i][1];
  }

  settings.openCloseMinTemp = Number(settings.openCloseMinTemp);
  settings.openCloseMaxTemp = Number(settings.openCloseMaxTemp);
  return settings;
}

function getLogsForDate_(ss, dateKey) {
  const sheet = ss.getSheetByName(CLEANING_CONFIG.sheets.logs);
  const values = sheet.getDataRange().getValues();
  const logs = [];

  for (let i = 1; i < values.length; i++) {
    if (String(values[i][1]) === dateKey) {
      logs.push(rowToLog_(values[i]));
    }
  }

  return logs;
}

function getRecentLogs_(ss, limit) {
  const sheet = ss.getSheetByName(CLEANING_CONFIG.sheets.logs);
  const lastRow = sheet.getLastRow();
  if (lastRow <= 1) return [];

  const start = Math.max(2, lastRow - limit + 1);
  const values = sheet.getRange(start, 1, lastRow - start + 1, sheet.getLastColumn()).getValues();
  return values.map(rowToLog_).reverse();
}

function rowToLog_(row) {
  return {
    timestamp: row[0] instanceof Date ? Utilities.formatDate(row[0], Session.getScriptTimeZone(), 'dd/MM/yyyy HH:mm') : row[0],
    date: row[1],
    type: row[2],
    area: row[3],
    slotLabel: row[4],
    staffName: row[5],
    value: row[6],
    status: row[7],
    notes: row[8],
    taskId: row[9]
  };
}

function buildTasksForDate_(settings, logs, now, ss) {
  const tasks = [];
  const openCloseTempAreas = getAreasByType_('Open/Close Temp', ss);
  const toiletAreas = getAreasByType_('Toilet Check', ss);

  openCloseTempAreas.forEach(area => {
    [
      { label: 'Opening Temp', time: String(settings.openTempDueBy || CLEANING_CONFIG.defaults.openTempDueBy) },
      { label: 'Closing Temp', time: String(settings.closeTempDueBy || CLEANING_CONFIG.defaults.closeTempDueBy) }
    ].forEach(slot => {
      tasks.push(createTask_({
        type: 'Open/Close Temp',
        area,
        slotLabel: slot.label,
        dueTime: slot.time,
        inputType: 'temperature',
        logs,
        now
      }));
    });
  });

  toiletAreas.forEach(area => {
    [
      { label: 'Open Check', time: String(settings.toiletOpenDueBy || CLEANING_CONFIG.defaults.toiletOpenDueBy) },
      { label: '2pm Check', time: String(settings.toiletTwoPmDueBy || CLEANING_CONFIG.defaults.toiletTwoPmDueBy) },
      { label: '6pm Check', time: String(settings.toiletSixPmDueBy || CLEANING_CONFIG.defaults.toiletSixPmDueBy) },
      { label: '8pm Check', time: String(settings.toiletEightPmDueBy || CLEANING_CONFIG.defaults.toiletEightPmDueBy) },
      { label: 'Close Check', time: String(settings.toiletCloseDueBy || CLEANING_CONFIG.defaults.toiletCloseDueBy) }
    ].forEach(slot => {
      tasks.push(createTask_({
        type: 'Toilet Check',
        area,
        slotLabel: slot.label,
        dueTime: slot.time,
        inputType: 'checklist',
        logs,
        now
      }));
    });
  });

  return tasks.sort((a, b) => a.sortMinutes - b.sortMinutes || a.area.localeCompare(b.area));
}

function createTask_(options) {
  const taskId = [
    todayKey_(),
    options.type,
    options.area,
    options.slotLabel
  ].join('|');
  const matchingLogs = options.logs.filter(log => log.taskId === taskId);
  const latest = matchingLogs.length ? matchingLogs[matchingLogs.length - 1] : null;
  const dueDate = dateFromTime_(options.dueTime, options.dueDayOffset || 0);
  const now = options.now || new Date();
  const minutesLate = Math.floor((now.getTime() - dueDate.getTime()) / 60000);
  const done = !!latest;
  const overdue = !done && minutesLate > 0;

  return {
    id: taskId,
    type: options.type,
    area: options.area,
    slotLabel: options.slotLabel,
    dueTime: options.dueTime,
    dueDisplay: formatTime_(options.dueTime),
    inputType: options.inputType,
    done,
    overdue,
    status: done ? latest.status : (overdue ? 'Overdue' : 'Due'),
    latest,
    sortMinutes: options.sortMinutes || timeToMinutes_(options.dueTime),
    minutesLate: Math.max(minutesLate, 0)
  };
}

function getAreasByType_(taskType, ss) {
  const sheet = ss.getSheetByName(CLEANING_CONFIG.sheets.templates);
  const values = sheet.getDataRange().getDisplayValues();
  const areas = [];

  for (let i = 1; i < values.length; i++) {
    if (values[i][0] === taskType && values[i][1]) {
      areas.push(String(values[i][1]));
    }
  }

  return areas;
}

function buildStats_(tasks, logs) {
  const total = tasks.length;
  const done = tasks.filter(task => task.done).length;
  const overdue = tasks.filter(task => task.overdue).length;
  const flagged = logs.filter(log => String(log.status).indexOf('Flag') === 0).length;

  return {
    total,
    done,
    due: total - done,
    overdue,
    flagged,
    completionPercent: total ? Math.round((done / total) * 100) : 0
  };
}

function getSubmittedStatus_(task, value, settings) {
  if (task.inputType === 'temperature') {
    const temp = Number(value);
    const min = settings.openCloseMinTemp;
    const max = settings.openCloseMaxTemp;
    if (isNaN(temp)) return 'Flag - No temp entered';
    if (temp < min || temp > max) {
      return 'Flag - Temp out of range';
    }
  }

  return 'Complete';
}

function getTodaysRotaStaff_(settings) {
  const spreadsheetId = cleanText_(settings.rotaSpreadsheetId);
  const sheetName = cleanText_(settings.rotaSheetName) || 'Shifts';

  if (!spreadsheetId) return [];

  try {
    const rotaSs = SpreadsheetApp.openById(extractSpreadsheetId_(spreadsheetId));
    const sheet = rotaSs.getSheetByName(sheetName);
    if (!sheet || sheet.getLastRow() < 2) return [];

    const displayValues = sheet.getDataRange().getDisplayValues();
    const headers = displayValues[0].map(header => cleanText_(header));
    const today = todayKey_();
    const seen = {};
    const staff = [];

    for (let i = 1; i < displayValues.length; i++) {
      const row = rowObject_(headers, displayValues[i]);
      const date = normaliseDateText_(row['Date']);
      if (date !== today) continue;

      const name = cleanText_(row['Staff Name'] || row['Person'] || row['Name']);
      if (!name) continue;
      if (!isWorkingShift_(row)) continue;
      if (seen[name.toLowerCase()]) continue;

      seen[name.toLowerCase()] = true;
      staff.push({
        name,
        label: buildRotaStaffLabel_(name, row)
      });
    }

    return staff.sort((a, b) => a.name.localeCompare(b.name));
  } catch (error) {
    return [{
      name: '',
      label: 'Could not read rota - check spreadsheet ID and permissions',
      error: error && error.message ? error.message : String(error)
    }];
  }
}

function rowObject_(headers, values) {
  const row = {};
  headers.forEach((header, index) => {
    if (header) row[header] = values[index];
  });
  return row;
}

function isWorkingShift_(row) {
  const combined = [
    row['Start Time'],
    row['End Time'],
    row['Shift 2 Start'],
    row['Shift 2 End'],
    row['Type'],
    row['Notes']
  ].map(cleanText_).join(' ').toLowerCase();

  if (!combined) return false;
  if (/\b(off|holiday|annual leave|rest day)\b/.test(combined)) return false;
  return true;
}

function buildRotaStaffLabel_(name, row) {
  const first = [row['Start Time'], row['End Time']].map(cleanText_).filter(Boolean).join('-');
  const second = [row['Shift 2 Start'], row['Shift 2 End']].map(cleanText_).filter(Boolean).join('-');
  const parts = [name];
  if (first) parts.push(first);
  if (second) parts.push(second);
  return parts.join(' - ');
}

function extractSpreadsheetId_(value) {
  const text = cleanText_(value);
  const match = text.match(/\/spreadsheets\/d\/([a-zA-Z0-9-_]+)/);
  return match ? match[1] : text;
}

function normaliseDateText_(value) {
  const textValue = cleanText_(value);
  if (!textValue) return '';

  const isoMatch = textValue.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (isoMatch) return `${isoMatch[1]}-${isoMatch[2]}-${isoMatch[3]}`;

  const ukMatch = textValue.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if (ukMatch) {
    return [
      ukMatch[3],
      String(ukMatch[2]).padStart(2, '0'),
      String(ukMatch[1]).padStart(2, '0')
    ].join('-');
  }

  const parsed = new Date(textValue);
  if (!isNaN(parsed.getTime())) {
    return Utilities.formatDate(parsed, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  }

  return textValue;
}

function cleanText_(value) {
  return String(value === null || value === undefined ? '' : value).trim();
}

function normaliseValue_(value) {
  if (Array.isArray(value)) return value.join('; ');
  if (value === undefined || value === null) return '';
  return String(value).trim();
}

function todayKey_() {
  return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
}

function todayDisplay_() {
  return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'EEEE d MMMM yyyy');
}

function dateFromTime_(time, dayOffset) {
  const now = new Date();
  const minutes = timeToMinutes_(time);
  const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() + (dayOffset || 0), 0, minutes, 0, 0);
  return date;
}

function timeToMinutes_(time) {
  const parts = String(time).split(':').map(Number);
  return (parts[0] || 0) * 60 + (parts[1] || 0);
}

function minutesToTime_(minutes) {
  const hh = Math.floor(minutes / 60).toString().padStart(2, '0');
  const mm = (minutes % 60).toString().padStart(2, '0');
  return `${hh}:${mm}`;
}

function formatTime_(time) {
  const minutes = timeToMinutes_(time);
  const date = new Date(2000, 0, 1, 0, minutes, 0, 0);
  return Utilities.formatDate(date, Session.getScriptTimeZone(), 'HH:mm');
}

function csvCell_(cell) {
  const value = cell instanceof Date
    ? Utilities.formatDate(cell, Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss')
    : String(cell === null || cell === undefined ? '' : cell);
  return `"${value.replace(/"/g, '""')}"`;
}

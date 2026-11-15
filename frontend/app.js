/*
 * Pi-Dashboard – Frontend-Logik (Plain JS, keine Abhängigkeiten).
 * Alle API-Strings werden ausschließlich per textContent eingefügt.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------
  // Konstanten
  // ------------------------------------------------------------------
  var WD_SHORT = ['So.', 'Mo.', 'Di.', 'Mi.', 'Do.', 'Fr.', 'Sa.'];
  var WD_LONG = ['Sonntag', 'Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag'];
  var MON_SHORT = ['Jan.', 'Feb.', 'März', 'Apr.', 'Mai', 'Juni', 'Juli', 'Aug.', 'Sept.', 'Okt.', 'Nov.', 'Dez.'];

  var PRIO_NAME = { 4: 'Priorität 1', 3: 'Priorität 2', 2: 'Priorität 3', 1: 'Priorität 4' };

  var STATUS_NAME = { free: 'Frei', busy: 'Beschäftigt', dnd: 'Nicht stören' };

  // Todoist-Projektfarben (Name -> Hex)
  var PROJECT_COLORS = {
    berry_red: '#B8255F', red: '#DC4C3E', orange: '#C77100', yellow: '#B29104',
    olive_green: '#949C31', lime_green: '#65A33A', green: '#369307', mint_green: '#42A393',
    teal: '#148FAD', sky_blue: '#319DC0', light_blue: '#6988A4', blue: '#4180FF',
    grape: '#692EC2', violet: '#CA3FEE', lavender: '#A4698C', magenta: '#E05095',
    salmon: '#C9766F', charcoal: '#5F5F5F', grey: '#808080', taupe: '#AF9E8E'
  };

  var DEFAULT_SETTINGS = {
    default_view: 'tasks',
    return_after_seconds: 120,
    theme: 'dark',
    priority_colors: {},
    status_colors: {}
  };

  var POLL_MS = 60000;          // Aufgaben + Kalender
  var POLL_AVAIL_MS = 15000;    // Verfügbarkeit
  var FETCH_TIMEOUT_MS = 8000;

  // ------------------------------------------------------------------
  // Zustand
  // ------------------------------------------------------------------
  var S = {
    settings: DEFAULT_SETTINGS,
    tz: null,                 // IANA-Zone aus /api/calendar (Anzeige)
    tasks: null,
    calendar: null,
    avail: null,
    view: 'tasks',
    failing: {},              // Endpunkt -> true, wenn letzter Abruf fehlschlug
    lastContact: null,        // Date des letzten erfolgreichen Abrufs
    flash: null,              // kurzzeitige Meldung (z. B. POST-Fehler)
    flashTimer: null,
    lastTouch: Date.now(),
    idleHandled: false,
    posting: 0,               // laufende POSTs (Polling überspringt dann Verfügbarkeit)
    postSeq: 0,
    sig: { tasks: '', calendar: '', summary: '' },
    lastMinute: '',
    lastDayKey: ''
  };

  // ------------------------------------------------------------------
  // DOM-Helfer
  // ------------------------------------------------------------------
  function $(id) { return document.getElementById(id); }

  /** Element erzeugen; text wird per textContent gesetzt (XSS-sicher). */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /** Eigenes Icon (vertrauenswürdiger SVG-String aus icons.js) einfügen. */
  function setIcon(node, name, size) { node.innerHTML = window.icon(name, size); }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  // ------------------------------------------------------------------
  // Zeit / Datum (Anzeigezone = calendar.timezone, sonst lokal)
  // ------------------------------------------------------------------
  var dtf = null, dtfTz;

  function makeFormatter(tz) {
    var o = { hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
              hour: '2-digit', minute: '2-digit', second: '2-digit' };
    if (tz) o.timeZone = tz;
    return new Intl.DateTimeFormat('en-GB', o);
  }

  /** Zerlegt ein Date in Zonen-Bestandteile. */
  function zoned(date) {
    if (!dtf || dtfTz !== S.tz) {
      dtfTz = S.tz;
      try { dtf = makeFormatter(S.tz); } catch (e) { dtf = makeFormatter(null); }
    }
    var p = {};
    dtf.formatToParts(date).forEach(function (x) { p[x.type] = x.value; });
    var y = +p.year, m = +p.month, d = +p.day;
    return {
      y: y, m: m, d: d,
      h: (+p.hour) % 24, mi: +p.minute, s: +p.second,
      key: p.year + '-' + p.month + '-' + p.day,
      dn: Date.UTC(y, m - 1, d) / 864e5       // fortlaufende Tagesnummer
    };
  }

  /** "YYYY-MM-DD" -> {y,m,d,dn,key} (ohne Zeitzonen-Umrechnung). */
  function parseDay(str) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(str || '');
    if (!m) return null;
    var y = +m[1], mo = +m[2], d = +m[3];
    return { y: y, m: mo, d: d, key: m[1] + '-' + m[2] + '-' + m[3], dn: Date.UTC(y, mo - 1, d) / 864e5 };
  }

  function weekdayOf(dn) { return new Date(dn * 864e5).getUTCDay(); }
  function fmtDate(p) { return p.d + '. ' + MON_SHORT[p.m - 1]; }
  function fmtHM(date) { var z = zoned(date); return pad(z.h) + ':' + pad(z.mi); }
  function today() { return zoned(new Date()); }

  /** "Mo., 17. Nov." */
  function fmtDayLong(p) { return WD_SHORT[weekdayOf(p.dn)] + ', ' + fmtDate(p); }

  // ------------------------------------------------------------------
  // Netzwerk
  // ------------------------------------------------------------------
  function api(path, opts) {
    var ctl = new AbortController();
    var t = setTimeout(function () { ctl.abort(); }, FETCH_TIMEOUT_MS);
    var o = Object.assign({ cache: 'no-store', signal: ctl.signal }, opts || {});
    return fetch(path, o).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (j) { clearTimeout(t); return j; },
            function (e) { clearTimeout(t); throw e; });
  }

  function markOk(name) {
    delete S.failing[name];
    S.lastContact = new Date();
    updateHint();
  }
  function markFail(name) {
    S.failing[name] = true;
    updateHint();
  }

  // ------------------------------------------------------------------
  // Hinweis (Snackbar)
  // ------------------------------------------------------------------
  function updateHint() {
    var msg = null;
    if (S.flash) {
      msg = S.flash;
    } else if (Object.keys(S.failing).length) {
      msg = 'Keine Verbindung zum Pi-Backend – zuletzt ' + (S.lastContact ? fmtHM(S.lastContact) : 'nie');
    } else {
      var parts = [];
      if (S.tasks && S.tasks.stale) parts.push('Todoist: zuletzt aktualisiert ' + fmtUpdated(S.tasks.updated_at));
      if (S.calendar && S.calendar.stale) parts.push('Kalender: zuletzt aktualisiert ' + fmtUpdated(S.calendar.updated_at));
      if (parts.length) msg = parts.join(' · ');
    }
    var bar = $('snackbar');
    if (!msg) {
      bar.hidden = true;
      document.body.classList.remove('hint-on');
      return;
    }
    bar.textContent = '';
    var ic = el('span');
    setIcon(ic, 'cloud_off', 18);
    bar.appendChild(ic);
    bar.appendChild(el('span', null, msg));
    bar.hidden = false;
    document.body.classList.add('hint-on');
  }

  function fmtUpdated(iso) {
    var d = iso ? new Date(iso) : null;
    return d && !isNaN(d) ? fmtHM(d) : 'unbekannt';
  }

  function flash(text, ms) {
    S.flash = text;
    clearTimeout(S.flashTimer);
    S.flashTimer = setTimeout(function () { S.flash = null; updateHint(); }, ms || 4000);
    updateHint();
  }

  // ------------------------------------------------------------------
  // Einstellungen
  // ------------------------------------------------------------------
  function applySettings(cfg) {
    S.settings = Object.assign({}, DEFAULT_SETTINGS, cfg || {});
    var root = document.documentElement;
    root.setAttribute('data-theme', S.settings.theme === 'light' ? 'light' : 'dark');
    var pc = S.settings.priority_colors || {};
    // Todoist-Priorität 4 = p1 ... 1 = p4
    [['p1', 4], ['p2', 3], ['p3', 2], ['p4', 1]].forEach(function (x) {
      if (pc[x[0]]) root.style.setProperty('--priority-' + x[1], pc[x[0]]);
    });
    var sc = S.settings.status_colors || {};
    ['free', 'busy', 'dnd'].forEach(function (k) {
      if (sc[k]) root.style.setProperty('--status-' + k, sc[k]);
    });
  }

  function loadSettings() {
    return api('/api/settings').then(function (cfg) {
      applySettings(cfg);
      markOk('settings');
    }).catch(function () {
      markFail('settings');
      return new Promise(function (res) { setTimeout(res, 2000); }).then(loadSettings);
    });
  }

  // ------------------------------------------------------------------
  // Aufgaben
  // ------------------------------------------------------------------
  function loadTasks() {
    return api('/api/tasks').then(function (d) {
      S.tasks = d;
      markOk('tasks');
      renderTasks();
      renderSummary();
    }).catch(function () { markFail('tasks'); });
  }

  /** Fälligkeits-Chip: Text relativ zu heute (deutsch). */
  function dueDayDiff(task) {
    var due = task.due;
    if (!due) return null;
    var p = parseDay(due.date || (due.datetime || '').slice(0, 10));
    return p ? p.dn - today().dn : null;
  }

  function dueText(task) {
    var due = task.due;
    if (!due) return null;
    var ds = due.date || (due.datetime || '').slice(0, 10);
    var p = parseDay(ds);
    if (!p) return due.string || null;
    var diff = dueDayDiff(task);
    var txt;
    if (diff === 0) txt = 'Heute';
    else if (diff === 1) txt = 'Morgen';
    else if (diff === -1) txt = 'Gestern';
    else if (diff >= 2 && diff <= 6) txt = WD_LONG[weekdayOf(p.dn)];
    else txt = fmtDate(p);
    if (due.datetime) {
      var time = timeOfDatetime(due.datetime);
      if (time) txt += ', ' + time;
    }
    return txt;
  }

  function timeOfDatetime(s) {
    var m = /T(\d{2}):(\d{2})/.exec(s);
    if (!m) return null;
    // Mit Offset/Z -> in Anzeigezone umrechnen, sonst (floating) Uhrzeit direkt
    if (/(Z|[+-]\d{2}:?\d{2})$/.test(s)) {
      var d = new Date(s);
      return isNaN(d) ? m[1] + ':' + m[2] : fmtHM(d);
    }
    return m[1] + ':' + m[2];
  }

  function chip(cls, text, iconName) {
    var c = el('span', 'chip ' + (cls || ''));
    if (iconName) { var i = el('span'); setIcon(i, iconName, 14); c.appendChild(i); }
    c.appendChild(el('span', null, text));
    return c;
  }

  function renderTasks() {
    var data = S.tasks;
    var sig = JSON.stringify(data && data.projects) + '|' + today().key;
    if (sig === S.sig.tasks) return;
    S.sig.tasks = sig;

    var grid = $('tasks-grid');
    var empty = $('tasks-empty');
    var projects = (data && data.projects) || [];
    grid.textContent = '';

    if (!projects.length) {
      empty.textContent = '';
      var ic = el('span');
      setIcon(ic, 'task_alt', 72);
      empty.appendChild(ic);
      empty.appendChild(el('div', null, 'Keine offenen Aufgaben'));
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    var frag = document.createDocumentFragment();
    projects.forEach(function (proj) {
      var tasks = proj.tasks || [];
      var card = el('section', 'card');
      var head = el('div', 'card-head');
      var dot = el('span', 'proj-dot');
      if (PROJECT_COLORS[proj.color]) dot.style.background = PROJECT_COLORS[proj.color];
      head.appendChild(dot);
      var titleBox = el('div', 'card-titlebox');
      var pathParts = [proj.parent, proj.section ? proj.name : null].filter(function (x) { return !!x; });
      if (pathParts.length) titleBox.appendChild(el('div', 'card-path', pathParts.join(' › ')));
      titleBox.appendChild(el('div', 'card-title', proj.section || proj.name || ''));
      head.appendChild(titleBox);
      head.appendChild(el('span', 'count', String(tasks.length)));
      card.appendChild(head);

      tasks.forEach(function (t) {
        var row = el('div', 'task');
        row.setAttribute('data-prio', String(t.priority >= 1 && t.priority <= 4 ? t.priority : 1));
        // Überfällig -> rot, heute fällig -> gelb (überfällig hat Vorrang)
        // Tagesvergleich zusätzlich zum Backend-Flag: nach Mitternacht ohne frische Daten korrekt
        var dayDiff = dueDayDiff(t);
        var overdue = !!t.overdue || (dayDiff !== null && dayDiff < 0);
        if (overdue) row.className += ' task-overdue';
        else if (dayDiff === 0) row.className += ' task-today';
        if (t.depth > 0) {
          row.className += ' subtask';
          row.style.setProperty('--depth', String(Math.min(t.depth, 4)));
        }
        row.appendChild(el('span', 'stripe'));
        var body = el('div', 'task-body');
        body.appendChild(el('div', 'task-content', t.content || ''));
        if (t.description) body.appendChild(el('div', 'task-desc', t.description));

        var chips = el('div', 'chips');
        var dt = dueText(t);
        if (dt) {
          var rec = t.due && t.due.is_recurring;
          var ico = t.due && t.due.datetime ? 'schedule' : (rec ? 'repeat' : null);
          chips.appendChild(chip(overdue ? 'chip-due chip-overdue' : 'chip-due', dt, ico));
        }
        (t.labels || []).forEach(function (l) { chips.appendChild(chip('', String(l))); });
        if (chips.childNodes.length) body.appendChild(chips);

        row.appendChild(body);
        card.appendChild(row);
      });
      frag.appendChild(card);
    });
    grid.appendChild(frag);
  }

  // ------------------------------------------------------------------
  // Kalender
  // ------------------------------------------------------------------
  function loadCalendar() {
    return api('/api/calendar').then(function (d) {
      S.calendar = d;
      if (d.timezone && d.timezone !== S.tz) { S.tz = d.timezone; S.sig.tasks = ''; renderTasks(); }
      markOk('calendar');
      renderCalendar();
      renderSummary();
    }).catch(function () { markFail('calendar'); });
  }

  function hourHeight() {
    var v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--hour-height'));
    return v > 0 ? v : 64;
  }

  /** Minuten seit 0:00 des Tages (todayKey) für einen Zeitpunkt, an den Tag geklemmt. */
  function minutesOnDay(iso, dayKey, isEnd) {
    var z = zoned(new Date(iso));
    if (z.key < dayKey) return 0;
    if (z.key > dayKey) return 1440;
    return z.h * 60 + z.mi;
  }

  /** Spalten für überlappende Termine bestimmen (nebeneinander). */
  function layoutColumns(evs) {
    evs.sort(function (a, b) { return a.s - b.s || b.e - a.e; });
    var colEnds = [], cluster = [], clusterEnd = -1;
    function flush() {
      var cols = 1;
      cluster.forEach(function (x) { cols = Math.max(cols, x.col + 1); });
      cluster.forEach(function (x) { x.cols = cols; });
      cluster = []; colEnds = []; clusterEnd = -1;
    }
    evs.forEach(function (ev) {
      if (cluster.length && ev.s >= clusterEnd) flush();
      var c = -1;
      for (var i = 0; i < colEnds.length; i++) { if (colEnds[i] <= ev.s) { c = i; break; } }
      if (c < 0) { c = colEnds.length; colEnds.push(0); }
      colEnds[c] = ev.le;
      ev.col = c;
      cluster.push(ev);
      clusterEnd = Math.max(clusterEnd, ev.le);
    });
    if (cluster.length) flush();
  }

  var tl = { startMin: 0, endMin: 0, hh: 64 };   // aktuelle Zeitleisten-Geometrie

  function renderCalendar() {
    var data = S.calendar;
    var t = today();
    var sig = JSON.stringify(data && data.days) + '|' + (data && data.day_start_hour) + '|' +
      (data && data.day_end_hour) + '|' + t.key + '|' + hourHeight();
    if (sig === S.sig.calendar) { updateNow(); return; }
    var firstRender = !S.sig.calendar;
    S.sig.calendar = sig;
    if (!data) return;

    var days = data.days || [];
    var todayEntry = null;
    days.forEach(function (d) { if (d.date === t.key) todayEntry = d; });
    if (!todayEntry && days.length) todayEntry = days[0];
    var dayKey = todayEntry ? todayEntry.date : t.key;
    var dayP = parseDay(dayKey) || t;

    $('today-title').textContent = 'Heute · ' + fmtDayLong(dayP);

    // --- ganztägige Termine + Zeitleisten-Termine
    var allDay = $('allday');
    allDay.textContent = '';
    var timed = [];
    ((todayEntry && todayEntry.events) || []).forEach(function (ev) {
      if (ev.all_day) {
        allDay.appendChild(chip(ev.busy === false ? 'free' : '', ev.title || '(ohne Titel)'));
      } else {
        var s = minutesOnDay(ev.start, dayKey), e = minutesOnDay(ev.end, dayKey, true);
        if (e <= s) e = Math.min(1440, s + 1);
        timed.push({ ev: ev, s: s, e: e, le: Math.max(e, s + 30) });
      }
    });

    // --- Bereich der Zeitleiste
    var startH = data.day_start_hour != null ? +data.day_start_hour : 7;
    var endH = data.day_end_hour != null ? +data.day_end_hour : 20;
    timed.forEach(function (x) {
      startH = Math.min(startH, Math.floor(x.s / 60));
      endH = Math.max(endH, Math.ceil(x.e / 60));
    });
    startH = Math.max(0, startH); endH = Math.min(24, Math.max(endH, startH + 1));
    var hh = hourHeight();
    tl = { startMin: startH * 60, endMin: endH * 60, hh: hh, dayKey: dayKey };

    var line = $('timeline');
    line.textContent = '';
    line.style.height = ((endH - startH) * hh) + 'px';
    for (var h = startH; h <= endH; h++) {
      var hl = el('div', 'hour');
      hl.style.top = ((h - startH) * hh) + 'px';
      hl.appendChild(el('span', 'hour-label', pad(h % 24) + ':00'));
      line.appendChild(hl);
    }

    var wrap = el('div', 'events');
    layoutColumns(timed);
    var nowMs = Date.now();
    timed.forEach(function (x) {
      var ev = x.ev;
      var top = (x.s - tl.startMin) / 60 * hh;
      var height = Math.max((x.e - x.s) / 60 * hh, 26) - 2;
      var n = el('div', 'event' + (ev.busy === false ? ' free' : '') +
        (new Date(ev.end).getTime() < nowMs ? ' past' : '') + (height < 44 ? ' short' : ''));
      n.style.top = (top + 1) + 'px';
      n.style.height = height + 'px';
      n.style.left = 'calc(' + (x.col / x.cols * 100) + '% + 1px)';
      n.style.width = 'calc(' + (100 / x.cols) + '% - 2px)';
      n.appendChild(el('div', 'ev-title', ev.title || '(ohne Titel)'));
      var sub = fmtHM(new Date(ev.start)) + '–' + fmtHM(new Date(ev.end)) +
        (ev.location ? ' · ' + ev.location : '');
      n.appendChild(el('div', 'ev-sub', sub));
      wrap.appendChild(n);
    });
    line.appendChild(wrap);

    if (!timed.length && !allDay.childNodes.length) {
      line.appendChild(el('div', 'tl-empty', 'Heute keine Termine'));
    }

    // --- Nächste Tage
    var next = $('next-days');
    next.textContent = '';
    var base = dayP.dn;
    days.forEach(function (d) {
      var p = parseDay(d.date);
      if (!p || p.dn <= base) return;
      var diff = p.dn - base;
      next.appendChild(el('div', 'day-head', diff === 1 ? 'Morgen' : fmtDayLong(p)));
      var evs = d.events || [];
      if (!evs.length) { next.appendChild(el('div', 'no-events', 'Keine Termine')); return; }
      evs.forEach(function (ev) {
        var row = el('div', 'ev-row' + (ev.busy === false ? ' free' : ''));
        row.appendChild(el('div', 't', ev.all_day ? 'ganztägig' : fmtHM(new Date(ev.start))));
        var n = el('div', 'n');
        n.appendChild(el('span', 'mark'));
        n.appendChild(el('span', null, ev.title || '(ohne Titel)'));
        row.appendChild(n);
        next.appendChild(row);
      });
    });

    updateNow();
    if (firstRender && S.view === 'calendar') scrollToNow();
  }

  /** "Jetzt"-Linie positionieren (jede Minute). */
  function updateNow() {
    var line = $('timeline');
    var old = line.querySelector('.now-line');
    if (old) old.remove();
    if (!S.calendar) return;
    var z = today();
    if (tl.dayKey && z.key !== tl.dayKey) return;
    var m = z.h * 60 + z.mi;
    if (m < tl.startMin || m > tl.endMin) return;
    var n = el('div', 'now-line');
    n.style.top = ((m - tl.startMin) / 60 * tl.hh) + 'px';
    n.appendChild(el('span', 'now-label', 'Jetzt'));
    line.appendChild(n);
  }

  /** Zeitleiste so scrollen, dass "Jetzt" sichtbar ist. */
  function scrollToNow() {
    var sc = $('timeline-scroll');
    var line = $('timeline').querySelector('.now-line');
    if (line) {
      var y = line.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop;
      sc.scrollTop = Math.max(0, y - sc.clientHeight / 3);
    } else {
      var first = $('timeline').querySelector('.event');
      sc.scrollTop = first ? Math.max(0, first.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop - 20) : 0;
    }
  }

  // ------------------------------------------------------------------
  // Tagesübersicht
  // ------------------------------------------------------------------
  /** Sortierschlüssel für die Uhrzeit einer Aufgabe (ohne Uhrzeit -> ans Ende). */
  function dueTimeKey(task) {
    var dt = task.due && task.due.datetime;
    return (dt && timeOfDatetime(dt)) || '99:99';
  }

  /** Alle heute fälligen und überfälligen Aufgaben, nach Wichtigkeit sortiert. */
  function summaryTasks() {
    var out = [];
    ((S.tasks && S.tasks.projects) || []).forEach(function (proj) {
      (proj.tasks || []).forEach(function (t) {
        var diff = dueDayDiff(t);
        var overdue = !!t.overdue || (diff !== null && diff < 0);
        if (!overdue && diff !== 0) return;
        out.push({
          t: t,
          proj: proj,
          prio: t.priority >= 1 && t.priority <= 4 ? t.priority : 1,
          overdue: overdue,
          diff: diff === null ? 0 : diff,
          time: dueTimeKey(t)
        });
      });
    });
    // Priorität absteigend; dann überfällig (älteste zuerst); dann Uhrzeit; dann Titel
    out.sort(function (a, b) {
      return (b.prio - a.prio) ||
        ((b.overdue ? 1 : 0) - (a.overdue ? 1 : 0)) ||
        (a.diff - b.diff) ||
        (a.time < b.time ? -1 : a.time > b.time ? 1 : 0) ||
        String(a.t.content || '').localeCompare(String(b.t.content || ''), 'de');
    });
    return out;
  }

  function renderSummary() {
    var t = today();
    var nowMin = t.h * 60 + t.mi;
    var cal = S.calendar;
    var todayEntry = null;
    ((cal && cal.days) || []).forEach(function (d) { if (d.date === t.key) todayEntry = d; });
    var evs = (todayEntry && todayEntry.events) || [];
    var items = summaryTasks();
    var sig = JSON.stringify(evs) + '|' +
      JSON.stringify(items.map(function (x) { return [x.t, x.proj.name, x.proj.section, x.proj.color]; })) +
      '|' + t.key + '|' + nowMin + '|' + !!cal + '|' + !!S.tasks;
    if (sig === S.sig.summary) return;
    S.sig.summary = sig;

    // --- Termine als Liste
    var evBox = $('sum-events');
    evBox.textContent = '';
    $('sum-events-title').textContent = 'Termine · ' + fmtDayLong(t);
    var nowMs = Date.now();
    var sorted = evs.slice().sort(function (a, b) {
      if (!!a.all_day !== !!b.all_day) return a.all_day ? -1 : 1;
      return new Date(a.start) - new Date(b.start);
    });
    if (!cal) evBox.appendChild(el('div', 'sum-empty', 'Kalender wird geladen …'));
    else if (!sorted.length) evBox.appendChild(el('div', 'sum-empty', 'Heute keine Termine'));
    sorted.forEach(function (ev) {
      var s = new Date(ev.start).getTime(), e = new Date(ev.end).getTime();
      var running = !ev.all_day && s <= nowMs && nowMs < e;
      var cls = 'sum-ev' + (ev.busy === false ? ' free' : '');
      if (!ev.all_day && e <= nowMs) cls += ' past';
      else if (running) cls += ' current';
      var row = el('div', cls);
      var time = el('div', 'sum-ev-time');
      if (ev.all_day) {
        time.appendChild(el('div', null, 'ganztägig'));
      } else {
        time.appendChild(el('div', null, fmtHM(new Date(ev.start))));
        time.appendChild(el('div', 'end', fmtHM(new Date(ev.end))));
      }
      row.appendChild(time);
      row.appendChild(el('span', 'sum-ev-bar'));
      var body = el('div', 'sum-ev-body');
      body.appendChild(el('div', 'sum-ev-title', ev.title || '(ohne Titel)'));
      var sub = [];
      if (running) sub.push('läuft gerade');
      if (ev.location) sub.push(ev.location);
      if (sub.length) body.appendChild(el('div', 'sum-ev-sub', sub.join(' · ')));
      row.appendChild(body);
      evBox.appendChild(row);
    });

    // --- Aufgaben, gruppiert nach Priorität
    var tBox = $('sum-tasks');
    tBox.textContent = '';
    $('sum-tasks-title').textContent = 'Aufgaben heute' + (items.length ? ' · ' + items.length : '');
    if (!S.tasks) tBox.appendChild(el('div', 'sum-empty', 'Aufgaben werden geladen …'));
    else if (!items.length) tBox.appendChild(el('div', 'sum-empty', 'Für heute ist nichts fällig'));
    var lastPrio = null;
    items.forEach(function (x) {
      if (x.prio !== lastPrio) {
        lastPrio = x.prio;
        var h = el('div', 'sum-prio-head');
        h.setAttribute('data-prio', String(x.prio));
        var ic = el('span');
        setIcon(ic, 'flag', 16);
        h.appendChild(ic);
        h.appendChild(el('span', null, PRIO_NAME[x.prio]));
        tBox.appendChild(h);
      }
      var row = el('div', 'task sum-task' + (x.overdue ? ' task-overdue' : ''));
      row.setAttribute('data-prio', String(x.prio));
      row.appendChild(el('span', 'stripe'));
      var body = el('div', 'task-body');
      body.appendChild(el('div', 'task-content', x.t.content || ''));
      var chips = el('div', 'chips');
      var dt = dueText(x.t);
      if (dt) {
        var ico = x.t.due && x.t.due.datetime ? 'schedule' : (x.t.due && x.t.due.is_recurring ? 'repeat' : null);
        chips.appendChild(chip(x.overdue ? 'chip-due chip-overdue' : 'chip-due', dt, ico));
      }
      var pname = [x.proj.name, x.proj.section].filter(function (v) { return !!v; }).join(' › ');
      if (pname) {
        var pc = chip('chip-proj', pname);
        var dot = el('span', 'proj-dot');
        if (PROJECT_COLORS[x.proj.color]) dot.style.background = PROJECT_COLORS[x.proj.color];
        pc.insertBefore(dot, pc.firstChild);
        chips.appendChild(pc);
      }
      body.appendChild(chips);
      row.appendChild(body);
      tBox.appendChild(row);
    });
  }

  // ------------------------------------------------------------------
  // Verfügbarkeit / Status
  // ------------------------------------------------------------------
  function loadAvailability() {
    if (S.posting) return Promise.resolve();   // keine Race mit laufendem POST
    return api('/api/availability').then(function (d) {
      if (S.posting) return;
      S.avail = d;
      markOk('availability');
      renderStatus();
    }).catch(function () { markFail('availability'); });
  }

  function renderStatus() {
    var a = S.avail;
    var line = $('status-line');
    line.textContent = '';
    document.querySelectorAll('.status-btn').forEach(function (b) {
      var isAuto = b.getAttribute('data-mode') === 'auto';
      var active = !!a && (isAuto ? a.mode === 'auto' : (a.mode === 'manual' && a.status === b.getAttribute('data-status')));
      var current = !!a && a.mode === 'auto' && !isAuto && a.status === b.getAttribute('data-status');
      b.classList.toggle('is-active', active);
      b.classList.toggle('is-current', current);
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    if (!a) return;

    var dot = el('span', 'dot');
    dot.style.setProperty('--c', 'var(--status-' + (STATUS_NAME[a.status] ? a.status : 'free') + ')');
    line.appendChild(dot);

    var txt = STATUS_NAME[a.status] || String(a.status || '');
    if (a.until) {
      var u = new Date(a.until);
      if (!isNaN(u)) txt += ' bis ' + fmtHM(u);
    }
    if (a.label && a.label !== STATUS_NAME[a.status]) txt += ' · ' + a.label;
    if (a.mode === 'manual') txt = 'Manuell: ' + txt;
    line.appendChild(el('span', 'txt', txt));

    if (a.mqtt_connected === false) {
      var off = el('span', 'mqtt-off');
      setIcon(off, 'cloud_off', 14);
      off.title = 'MQTT getrennt';
      line.appendChild(off);
    }
  }

  /** Statusbutton gedrückt: optimistisch anzeigen, POST, bei Fehler zurück. */
  function setMode(mode, status) {
    var prev = S.avail;
    var seq = ++S.postSeq;
    var body = mode === 'auto' ? { mode: 'auto' } : { mode: 'manual', status: status };

    var opt = Object.assign({}, prev || {}, { mode: mode });
    if (mode === 'manual') {
      opt.status = status; opt.until = null; opt.label = STATUS_NAME[status];
    }
    S.avail = opt;
    renderStatus();

    S.posting++;
    api('/api/availability', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (res) {
      if (seq === S.postSeq) { S.avail = res; renderStatus(); }
      markOk('availability');
    }).catch(function () {
      if (seq === S.postSeq) { S.avail = prev; renderStatus(); }
      flash('Status konnte nicht geändert werden');
    }).then(function () { S.posting--; });
  }

  // ------------------------------------------------------------------
  // Ansichten & Rückkehr-Timer
  // ------------------------------------------------------------------
  function setView(v) {
    S.view = v;
    $('view-tasks').hidden = v !== 'tasks';
    $('view-calendar').hidden = v !== 'calendar';
    $('view-summary').hidden = v !== 'summary';
    document.querySelectorAll('.seg-btn').forEach(function (b) {
      b.setAttribute('aria-selected', b.getAttribute('data-view') === v ? 'true' : 'false');
    });
    if (v === 'calendar') { renderCalendar(); scrollToNow(); $('next-scroll').scrollTop = 0; }
    if (v === 'summary') renderSummary();
  }

  function defaultView() {
    var v = S.settings.default_view;
    return v === 'calendar' || v === 'summary' ? v : 'tasks';
  }

  function onIdle() {
    setView(defaultView());
    $('view-tasks').scrollTop = 0;
    $('next-scroll').scrollTop = 0;
    $('sum-events-scroll').scrollTop = 0;
    $('sum-tasks-scroll').scrollTop = 0;
    // Zeitleiste: "Jetzt" statt ganz oben, sonst wäre die Gegenwart verdeckt
    if (S.view === 'calendar') scrollToNow(); else $('timeline-scroll').scrollTop = 0;
  }

  function checkIdle() {
    var sec = +S.settings.return_after_seconds;
    if (!(sec > 0) || S.idleHandled) return;
    if (Date.now() - S.lastTouch >= sec * 1000) {
      S.idleHandled = true;
      onIdle();
    }
  }

  // ------------------------------------------------------------------
  // Wisch-Scrollen ohne echte Touch-Events
  // ------------------------------------------------------------------
  /*
   * Liefert der Touchscreen im Kiosk/Vollbild nur Maus-Events (z. B. labwc-
   * Mausemulation oder Chromium ohne Touch-Erkennung), scrollt der Browser beim
   * Ziehen nicht von selbst. Dann übernimmt dieses Ziehen-zum-Scrollen inkl.
   * Nachlauf. Echte Touch-Eingaben (pointerType "touch") scrollt der Browser
   * weiterhin nativ.
   */
  function initDragScroll() {
    var SLOP = 6;            // px, ab hier gilt die Geste als Wischen
    var drag = null, raf = 0, swallowClick = false;

    function scrollerOf(node) {
      return node && node.closest ? node.closest('.view-tasks, .panel-scroll') : null;
    }

    function end(e, fling) {
      if (!drag || e.pointerId !== drag.id) return;
      var d = drag;
      drag = null;
      if (!d.moved) return;
      document.body.classList.remove('dragging');
      try { d.sc.releasePointerCapture(d.id); } catch (err) { /* egal */ }
      swallowClick = true;
      setTimeout(function () { swallowClick = false; }, 0);
      if (!fling || Date.now() - d.t > 80) return;    // Finger stand vor dem Loslassen still
      var v = d.v, last = Date.now();                 // px/ms
      (function step() {
        var now = Date.now(), dt = now - last;
        last = now;
        var before = d.sc.scrollTop;
        d.sc.scrollTop = before - v * dt;
        v *= Math.pow(0.995, dt);
        if (Math.abs(v) > 0.03 && d.sc.scrollTop !== before) raf = requestAnimationFrame(step);
      })();
    }

    document.addEventListener('pointerdown', function (e) {
      cancelAnimationFrame(raf);                      // Antippen stoppt den Nachlauf
      drag = null;
      if (e.pointerType === 'touch' || e.button !== 0) return;
      var sc = scrollerOf(e.target);
      if (!sc || sc.scrollHeight <= sc.clientHeight) return;
      drag = { sc: sc, id: e.pointerId, y0: e.clientY, top0: sc.scrollTop,
               moved: false, y: e.clientY, t: Date.now(), v: 0 };
    });

    document.addEventListener('pointermove', function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      var dy = e.clientY - drag.y0;
      if (!drag.moved) {
        if (Math.abs(dy) < SLOP) return;
        drag.moved = true;
        document.body.classList.add('dragging');
        try { drag.sc.setPointerCapture(drag.id); } catch (err) { /* egal */ }
      }
      drag.sc.scrollTop = drag.top0 - dy;
      var now = Date.now(), dt = now - drag.t;
      if (dt > 0) {
        drag.v = 0.7 * ((e.clientY - drag.y) / dt) + 0.3 * drag.v;
        drag.y = e.clientY;
        drag.t = now;
      }
      S.lastTouch = now;                              // Wischen hält den Rückkehr-Timer an
      e.preventDefault();
    });

    document.addEventListener('pointerup', function (e) { end(e, true); });
    document.addEventListener('pointercancel', function (e) { end(e, false); });
    // Nach einem Wisch keinen Klick auslösen
    document.addEventListener('click', function (e) {
      if (swallowClick) { e.stopPropagation(); e.preventDefault(); }
    }, true);
    // Kein natives Drag&Drop (würde das Wischen abbrechen)
    document.addEventListener('dragstart', function (e) { e.preventDefault(); });
  }

  // ------------------------------------------------------------------
  // Pull-Down-Panel (Schnelleinstellungen)
  // ------------------------------------------------------------------
  /*
   * Wischen nach unten in der App-Bar öffnet das Panel, es folgt dem Finger
   * (translateY). Die Geste startet erst nach SLOP px überwiegend vertikaler
   * Bewegung; vorher bleibt ein Tipp auf Buttons ein normaler Klick.
   * Einrasten: Öffnen ab 35 % Höhe oder 0,5 px/ms nach unten; Schließen ab
   * 25 % Weg oder 0,5 px/ms nach oben.
   */
  function loadNetwork() {
    var ul = $('net-list');
    fetch('/api/network', { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (d) {
      ul.textContent = '';
      var list = (d && d.interfaces) || [];
      if (!list.length) { ul.appendChild(el('li', 'none', 'Keine Verbindung')); return; }
      list.forEach(function (i) {
        var li = el('li');
        li.appendChild(el('span', 'if', i.name + ' – '));
        li.appendChild(document.createTextNode(i.address));
        ul.appendChild(li);
      });
    }).catch(function () {
      ul.textContent = '';
      ul.appendChild(el('li', 'none', 'Netzwerk nicht verfügbar'));
    });
  }

  function initPullDown() {
    var SLOP = 8, OPEN_FRAC = 0.35, CLOSE_FRAC = 0.25, FLING = 0.5;
    var panel = $('qs-panel'), scrim = $('qs-scrim'), bar = document.querySelector('header.appbar');
    var isOpen = false, g = null, swallow = false;

    function height() { return panel.offsetHeight || 1; }

    function place(y, animate) {            // y: -h (zu) … 0 (offen)
      var h = height();
      panel.classList[animate ? 'add' : 'remove']('anim');
      scrim.classList[animate ? 'add' : 'remove']('anim');
      panel.style.transform = 'translateY(' + y + 'px)';
      scrim.style.opacity = String(Math.max(0, Math.min(1, 1 + y / h)));
    }

    function settle(open) {
      isOpen = open;
      panel.setAttribute('aria-hidden', open ? 'false' : 'true');
      place(open ? 0 : -height(), true);
      if (open) loadNetwork();
      else setTimeout(function () {
        if (!isOpen && !g) { panel.style.visibility = ''; scrim.hidden = true; }
      }, 240);
    }

    function show() {
      panel.style.visibility = 'visible';
      scrim.hidden = false;
    }

    function start(e, opening) {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      g = { id: e.pointerId, x0: e.clientX, y0: e.clientY, y: e.clientY, t: Date.now(),
            v: 0, active: false, el: e.currentTarget, base: isOpen ? 0 : -height(), opening: opening };
    }

    bar.addEventListener('pointerdown', function (e) { if (!isOpen) start(e, true); });
    panel.addEventListener('pointerdown', function (e) { if (isOpen) start(e, false); });

    document.addEventListener('pointermove', function (e) {
      if (!g || e.pointerId !== g.id) return;
      var dx = e.clientX - g.x0, dy = e.clientY - g.y0;
      if (!g.active) {
        if (Math.abs(dy) < SLOP || Math.abs(dy) < Math.abs(dx)) return;
        if (g.opening ? dy < 0 : dy > 0) { g = null; return; }   // falsche Richtung: keine Geste
        g.active = true;
        g.y0 = e.clientY; g.y = e.clientY; g.t = Date.now();     // ab hier folgt das Panel
        document.body.classList.add('dragging');
        try { g.el.setPointerCapture(g.id); } catch (err) { /* egal */ }
        if (g.opening) show();
        place(g.base, false);
        return;
      }
      var y = Math.max(-height(), Math.min(0, g.base + (e.clientY - g.y0)));
      place(y, false);
      var now = Date.now(), dt = now - g.t;
      if (dt > 0) { g.v = 0.7 * ((e.clientY - g.y) / dt) + 0.3 * g.v; g.y = e.clientY; g.t = now; }
      S.lastTouch = now;
      e.preventDefault();
    });

    function finish(e, cancelled) {
      if (!g || e.pointerId !== g.id) return;
      var d = g; g = null;
      if (!d.active) return;
      document.body.classList.remove('dragging');
      try { d.el.releasePointerCapture(d.id); } catch (err) { /* egal */ }
      swallow = true;
      setTimeout(function () { swallow = false; }, 0);
      var h = height();
      var y = Math.max(-h, Math.min(0, d.base + (e.clientY - d.y0)));
      var v = Date.now() - d.t > 80 ? 0 : d.v;    // Finger stand vor dem Loslassen still
      var open;
      if (cancelled) open = isOpen;
      else if (d.opening) open = v > FLING || (h + y) / h > OPEN_FRAC;
      else open = !(v < -FLING || -y / h > CLOSE_FRAC);
      settle(open);
    }
    document.addEventListener('pointerup', function (e) { finish(e, false); });
    document.addEventListener('pointercancel', function (e) { finish(e, true); });
    document.addEventListener('click', function (e) {
      if (swallow) { e.stopPropagation(); e.preventDefault(); }
    }, true);

    scrim.addEventListener('click', function () { settle(false); });
    $('qs-close').addEventListener('click', function () { settle(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && isOpen) settle(false); });
    window.addEventListener('resize', function () { if (!isOpen) place(-height(), false); });
    place(-height(), false);
  }

  // ------------------------------------------------------------------
  // Uhr
  // ------------------------------------------------------------------
  function tickClock() {
    var now = new Date();
    var z = zoned(now);
    var hm = pad(z.h) + ':' + pad(z.mi);
    if (hm !== S.lastMinute) {
      S.lastMinute = hm;
      $('clock').textContent = hm;
      var dEl = $('date');
      dEl.textContent = '';
      dEl.appendChild(el('span', 'wd', WD_SHORT[weekdayOf(z.dn)] + ', '));
      dEl.appendChild(document.createTextNode(fmtDate(z)));
      if (S.calendar) renderCalendar();   // verschiebt die Jetzt-Linie (bzw. baut bei Tageswechsel neu)
      if (S.tasks) renderTasks();         // "Heute/Morgen" bei Tageswechsel
      renderSummary();                    // vergangene/laufende Termine, Tageswechsel
      if (S.avail) renderStatus();
    }
    checkIdle();
  }

  // ------------------------------------------------------------------
  // Start
  // ------------------------------------------------------------------
  function init() {
    // Icons einsetzen
    document.querySelectorAll('[data-icon]').forEach(function (n) {
      setIcon(n, n.getAttribute('data-icon'), 24);
    });

    // Kein Kontextmenü bei langem Druck
    document.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    // Jede Berührung setzt den Rückkehr-Timer zurück
    document.addEventListener('pointerdown', function () {
      S.lastTouch = Date.now();
      S.idleHandled = false;
    }, true);
    initDragScroll();
    initPullDown();

    document.querySelectorAll('.seg-btn').forEach(function (b) {
      b.addEventListener('click', function () { setView(b.getAttribute('data-view')); });
    });
    document.querySelectorAll('.status-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        setMode(b.getAttribute('data-mode'), b.getAttribute('data-status'));
      });
    });

    tickClock();
    setInterval(tickClock, 1000);

    loadSettings().then(function () {
      setView(defaultView());
      loadTasks(); loadCalendar(); loadAvailability();
      setInterval(function () { loadTasks(); loadCalendar(); }, POLL_MS);
      setInterval(loadAvailability, POLL_AVAIL_MS);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

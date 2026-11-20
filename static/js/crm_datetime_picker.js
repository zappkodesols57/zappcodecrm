/**
 * ZappCode CRM - Standard DateTime Engine
 * Provides a unified, single 12-Hour Continuous AM/PM Time Picker
 * and Standard ISO/Formatted Date Picker across all Hospital and Academy forms.
 */

(function (window, document) {
  'use strict';

  const shortMonthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // ==========================================
  // 1. STANDARD TIME PICKER MODULE
  // ==========================================
  const TimePicker = {
    popover: null,
    activeInput: null,
    selHour: "09",
    selMin: "00",
    selAmPm: "AM",

    parseTimeVal(val) {
      if (!val) return { h: "09", m: "00", p: "AM" };
      val = val.trim();
      const match12 = val.match(/^(\d{1,2}):(\d{2})\s*(AM|PM)?$/i);
      if (match12) {
        let h = parseInt(match12[1], 10);
        let m = match12[2];
        let p = (match12[3] || "AM").toUpperCase();
        if (h < 1) h = 12;
        if (h > 12) h = 12;
        return { h: String(h).padStart(2, '0'), m: m, p: p };
      }
      const match24 = val.match(/^(\d{1,2}):(\d{2})$/);
      if (match24) {
        let h24 = parseInt(match24[1], 10);
        let m = match24[2];
        let p = h24 >= 12 ? "PM" : "AM";
        let h12 = h24 % 12;
        if (h12 === 0) h12 = 12;
        return { h: String(h12).padStart(2, '0'), m: m, p: p };
      }
      return { h: "09", m: "00", p: "AM" };
    },

    ensurePopover() {
      if (this.popover) return this.popover;
      const popover = document.createElement("div");
      popover.className = "custom-12h-time-picker-popover";
      popover.id = "global12hTimePicker";
      popover.innerHTML = `
        <div class="d-flex align-items-stretch" style="gap: 8px;">
          <div class="time-picker-col">
            <div class="time-picker-col-title">Hour</div>
            <div class="time-picker-scroll-list" id="pickerHoursList">
              ${Array.from({length: 12}, (_, i) => {
                const h = String(i + 1).padStart(2, '0');
                return `<div class="time-picker-cell" data-type="hour" data-val="${h}">${h}</div>`;
              }).join('')}
            </div>
          </div>
          <div class="time-picker-col">
            <div class="time-picker-col-title">Minute</div>
            <div class="time-picker-scroll-list" id="pickerMinutesList">
              ${Array.from({length: 60}, (_, i) => {
                const m = String(i).padStart(2, '0');
                return `<div class="time-picker-cell" data-type="minute" data-val="${m}">${m}</div>`;
              }).join('')}
            </div>
          </div>
          <div class="ampm-toggle-col">
            <button type="button" class="ampm-btn" data-val="AM">AM</button>
            <button type="button" class="ampm-btn" data-val="PM">PM</button>
            <button type="button" class="btn btn-sm btn-primary mt-auto py-1 px-2 fw-semibold" id="pickerDoneBtn" style="font-size: 0.72rem; border-radius: 6px;">Done</button>
          </div>
        </div>
      `;
      document.body.appendChild(popover);
      this.popover = popover;

      popover.addEventListener("click", (e) => {
        const cell = e.target.closest(".time-picker-cell");
        if (cell) {
          if (cell.dataset.type === "hour") this.selHour = cell.dataset.val;
          else if (cell.dataset.type === "minute") this.selMin = cell.dataset.val;
          this.commit();
          this.updateUI();
          return;
        }
        const ampmBtn = e.target.closest(".ampm-btn");
        if (ampmBtn) {
          this.selAmPm = ampmBtn.dataset.val;
          this.commit();
          this.updateUI();
          return;
        }
        if (e.target.id === "pickerDoneBtn") {
          this.commit();
          this.hide();
        }
      });

      document.addEventListener("click", (e) => {
        if (this.popover && this.popover.style.display !== "none") {
          if (!this.popover.contains(e.target) && (!this.activeInput || !this.activeInput.contains(e.target))) {
            this.hide();
          }
        }
      }, true);

      return popover;
    },

    updateUI() {
      if (!this.popover) return;
      this.popover.querySelectorAll("#pickerHoursList .time-picker-cell").forEach(cell => {
        const isActive = cell.dataset.val === this.selHour;
        cell.classList.toggle("active", isActive);
        if (isActive) cell.scrollIntoView({ block: "center", behavior: "auto" });
      });
      this.popover.querySelectorAll("#pickerMinutesList .time-picker-cell").forEach(cell => {
        const isActive = cell.dataset.val === this.selMin;
        cell.classList.toggle("active", isActive);
        if (isActive) cell.scrollIntoView({ block: "center", behavior: "auto" });
      });
      this.popover.querySelectorAll(".ampm-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.val === this.selAmPm);
      });
    },

    commit() {
      if (!this.activeInput) return;
      // Always display user-friendly 12-hour AM/PM format (e.g. 03:30 PM) directly in the input field
      this.activeInput.value = `${this.selHour}:${this.selMin} ${this.selAmPm}`;
      this.activeInput.setAttribute('value', this.activeInput.value);
      this.activeInput.dispatchEvent(new Event('input', { bubbles: true }));
      this.activeInput.dispatchEvent(new Event('change', { bubbles: true }));
    },

    to24Hour(hStr, ampm) {
      let h = parseInt(hStr, 10);
      if (ampm === "PM" && h < 12) h += 12;
      if (ampm === "AM" && h === 12) h = 0;
      return String(h).padStart(2, '0');
    },

    reposition() {
      if (!this.popover || !this.activeInput || this.popover.style.display === "none") return;
      const rect = this.activeInput.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) {
        this.hide();
        return;
      }
      let topPos = rect.bottom + 6;
      if (topPos + 220 > window.innerHeight) {
        topPos = Math.max(10, rect.top - 230);
      }
      this.popover.style.top = `${topPos}px`;
      this.popover.style.left = `${Math.max(10, Math.min(rect.left, window.innerWidth - 265))}px`;
    },

    show(input) {
      this.activeInput = input;
      this.ensurePopover();
      const parsed = this.parseTimeVal(input.value);
      this.selHour = parsed.h;
      this.selMin = parsed.m;
      this.selAmPm = parsed.p;

      this.popover.style.display = "block";
      this.reposition();
      this.updateUI();
    },

    hide() {
      if (this.popover) this.popover.style.display = "none";
      this.activeInput = null;
    }
  };

  // ==========================================
  // 2. STANDARD DATE PICKER MODULE
  // ==========================================
  const DatePicker = {
    popover: null,
    activeInput: null,
    calYear: new Date().getFullYear(),
    calMonth: new Date().getMonth(),

    formatToReadable(isoStr) {
      if (!isoStr) return "";
      try {
        const parts = isoStr.split("-");
        if (parts.length === 3) {
          const y = parts[0];
          const m = parseInt(parts[1], 10) - 1;
          const d = parseInt(parts[2], 10);
          return `${String(d).padStart(2, '0')} ${shortMonthNames[m] || ''} ${y}`;
        }
      } catch(e) {}
      return isoStr;
    },

    parseToIso(readableStr) {
      if (!readableStr) return "";
      readableStr = readableStr.trim();
      if (/^\d{4}-\d{2}-\d{2}$/.test(readableStr)) return readableStr;
      const match = readableStr.match(/^(\d{1,2})\s+([A-Za-z]{3,4})\s+(\d{4})$/);
      if (match) {
        const d = String(parseInt(match[1], 10)).padStart(2, '0');
        const mIdx = shortMonthNames.findIndex(mn => mn.toLowerCase() === match[2].substring(0, 3).toLowerCase());
        if (mIdx !== -1) {
          const m = String(mIdx + 1).padStart(2, '0');
          return `${match[3]}-${m}-${d}`;
        }
      }
      return readableStr;
    },

    ensurePopover() {
      if (this.popover) return this.popover;
      const popover = document.createElement("div");
      popover.className = "global-date-picker-popover";
      popover.id = "globalUniversalDatePicker";
      popover.innerHTML = `
        <div class="global-cal-header">
          <span class="global-cal-month-title" id="globalCalMonthTitle">Month Year</span>
          <div class="d-flex align-items-center gap-1">
            <button type="button" class="btn btn-sm btn-light border py-0 px-2 rounded-2 text-secondary" style="font-size: 0.72rem; height: 26px;" id="globalCalPrevBtn">
              <i class="fa-solid fa-chevron-left"></i>
            </button>
            <button type="button" class="btn btn-sm btn-light border py-0 px-2 rounded-2 text-secondary" style="font-size: 0.72rem; height: 26px;" id="globalCalNextBtn">
              <i class="fa-solid fa-chevron-right"></i>
            </button>
          </div>
        </div>
        <div class="global-cal-grid" id="globalCalGrid"></div>
        <div class="global-cal-footer">
          <button type="button" class="global-cal-quick-btn" id="globalCalTodayBtn">Today</button>
          <button type="button" class="global-cal-quick-btn" id="globalCalTomorrowBtn">Tomorrow</button>
          <button type="button" class="global-cal-quick-btn" id="globalCalClearBtn">Clear</button>
        </div>
      `;
      document.body.appendChild(popover);
      this.popover = popover;

      document.getElementById("globalCalPrevBtn").addEventListener("click", () => {
        this.calMonth--;
        if (this.calMonth < 0) { this.calMonth = 11; this.calYear--; }
        this.renderCalendar();
      });

      document.getElementById("globalCalNextBtn").addEventListener("click", () => {
        this.calMonth++;
        if (this.calMonth > 11) { this.calMonth = 0; this.calYear++; }
        this.renderCalendar();
      });

      document.getElementById("globalCalTodayBtn").addEventListener("click", () => {
        const today = new Date();
        const iso = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
        this.selectDate(iso);
      });

      document.getElementById("globalCalTomorrowBtn").addEventListener("click", () => {
        const d = new Date();
        d.setDate(d.getDate() + 1);
        const iso = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        this.selectDate(iso);
      });

      document.getElementById("globalCalClearBtn").addEventListener("click", () => {
        if (this.activeInput) {
          this.activeInput.value = "";
          this.activeInput.dispatchEvent(new Event('input', { bubbles: true }));
          this.activeInput.dispatchEvent(new Event('change', { bubbles: true }));
        }
        this.hide();
      });

      document.addEventListener("click", (e) => {
        if (this.popover && this.popover.style.display !== "none") {
          if (!this.popover.contains(e.target) && (!this.activeInput || !this.activeInput.contains(e.target))) {
            this.hide();
          }
        }
      }, true);

      return popover;
    },

    renderCalendar() {
      if (!this.popover) return;
      const titleElem = document.getElementById("globalCalMonthTitle");
      const gridElem = document.getElementById("globalCalGrid");
      titleElem.textContent = `${shortMonthNames[this.calMonth]} ${this.calYear}`;

      gridElem.innerHTML = "";
      const weekDays = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
      weekDays.forEach(w => {
        const d = document.createElement("div");
        d.className = "global-cal-day-name";
        d.textContent = w;
        gridElem.appendChild(d);
      });

      const firstDayIdx = new Date(this.calYear, this.calMonth, 1).getDay();
      const totalDays = new Date(this.calYear, this.calMonth + 1, 0).getDate();
      const prevMonthDays = new Date(this.calYear, this.calMonth, 0).getDate();

      const today = new Date();
      const todayIso = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
      const selectedIso = this.activeInput ? this.parseToIso(this.activeInput.value) : "";

      for (let i = firstDayIdx - 1; i >= 0; i--) {
        const d = document.createElement("div");
        d.className = "global-cal-cell other-month";
        d.textContent = prevMonthDays - i;
        gridElem.appendChild(d);
      }

      for (let day = 1; day <= totalDays; day++) {
        const cellIso = `${this.calYear}-${String(this.calMonth + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const d = document.createElement("div");
        d.className = "global-cal-cell";
        d.textContent = day;

        if (cellIso === todayIso) d.classList.add("today");
        if (cellIso === selectedIso) d.classList.add("selected");

        d.addEventListener("click", () => this.selectDate(cellIso));
        gridElem.appendChild(d);
      }
    },

    selectDate(isoDate) {
      if (!this.activeInput) return;
      if (this.activeInput.type === "date") {
        this.activeInput.value = isoDate;
      } else {
        this.activeInput.value = this.formatToReadable(isoDate);
      }
      this.activeInput.dispatchEvent(new Event('input', { bubbles: true }));
      this.activeInput.dispatchEvent(new Event('change', { bubbles: true }));
      this.hide();
    },

    reposition() {
      if (!this.popover || !this.activeInput || this.popover.style.display === "none") return;
      const rect = this.activeInput.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) {
        this.hide();
        return;
      }
      let topPos = rect.bottom + 6;
      if (topPos + 290 > window.innerHeight) {
        topPos = Math.max(10, rect.top - 300);
      }
      this.popover.style.top = `${topPos}px`;
      this.popover.style.left = `${Math.max(10, Math.min(rect.left, window.innerWidth - 270))}px`;
    },

    show(input) {
      this.activeInput = input;
      this.ensurePopover();
      const currentVal = input.value || "";
      const parsedIso = this.parseToIso(currentVal);
      if (parsedIso && parsedIso.split("-").length === 3) {
        const parts = parsedIso.split("-");
        this.calYear = parseInt(parts[0], 10);
        this.calMonth = parseInt(parts[1], 10) - 1;
      } else {
        const now = new Date();
        this.calYear = now.getFullYear();
        this.calMonth = now.getMonth();
      }
      this.popover.style.display = "block";
      this.reposition();
      this.renderCalendar();
    },

    hide() {
      if (this.popover) this.popover.style.display = "none";
      this.activeInput = null;
    }
  };

  // ==========================================
  // 3. GLOBAL AUTO-ATTACH & FORM NORMALIZATION
  // ==========================================
  function initDateTimePickers() {
    window.addEventListener("scroll", () => {
      TimePicker.reposition();
      DatePicker.reposition();
    }, true);

    window.addEventListener("resize", () => {
      TimePicker.reposition();
      DatePicker.reposition();
    });

    // Time input trigger
    document.addEventListener("focus", (e) => {
      const target = e.target;
      if (!target) return;
      if (target.matches('.time-mask-input, .custom-12h-time-input') || (target.tagName === 'INPUT' && target.type === 'time')) {
        e.preventDefault();
        target.setAttribute('autocomplete', 'off');
        target.style.cursor = 'pointer';
        TimePicker.show(target);
      } else if (target.matches('.custom-date-picker-input') || (target.tagName === 'INPUT' && target.type === 'date')) {
        e.preventDefault();
        target.setAttribute('autocomplete', 'off');
        target.style.cursor = 'pointer';
        DatePicker.show(target);
      }
    }, true);

    document.addEventListener("click", (e) => {
      const timeInp = e.target.closest('.time-mask-input, .custom-12h-time-input, input[type="time"]');
      if (timeInp) {
        e.preventDefault();
        e.stopPropagation();
        TimePicker.show(timeInp);
        return;
      }
      const dateInp = e.target.closest('.custom-date-picker-input, input[type="date"]');
      if (dateInp) {
        e.preventDefault();
        e.stopPropagation();
        DatePicker.show(dateInp);
        return;
      }
    });

    // Normalize values before any form submission
    document.addEventListener("submit", (e) => {
      const form = e.target;
      // Convert 12-hour AM/PM to standard 24-hour HH:MM:00 for Django backends
      form.querySelectorAll('.time-mask-input, .custom-12h-time-input, input[type="time"]').forEach(inp => {
        let v = (inp.value || "").trim();
        if (v && /AM|PM/i.test(v)) {
          const p = TimePicker.parseTimeVal(v);
          let h = parseInt(p.h, 10);
          if (p.p === "PM" && h < 12) h += 12;
          if (p.p === "AM" && h === 12) h = 0;
          inp.value = `${String(h).padStart(2, '0')}:${p.m}:00`;
        }
      });
      // Convert formatted dates e.g. "15 Aug 2026" to "YYYY-MM-DD"
      form.querySelectorAll('.custom-date-picker-input').forEach(inp => {
        inp.value = DatePicker.parseToIso(inp.value);
      });
    }, true);
  }

  // Expose standard API globally
  window.ZappCodeDateTime = {
    TimePicker,
    DatePicker,
    init: initDateTimePickers
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDateTimePickers);
  } else {
    initDateTimePickers();
  }

})(window, document);

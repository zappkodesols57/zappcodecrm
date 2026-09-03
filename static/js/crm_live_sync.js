/**
 * Zappcode CRM - Live Auto-Refresh & Background Polling Client
 * 
 * Features:
 * - Periodically fetches live metrics every 30 seconds
 * - Pauses automatically when tab is hidden or user is typing / in active modal
 * - Safe: Never reloads page or overwrites active forms
 * - Safe: Never logs user out or causes session drops
 * - Graceful: Silently retries if remote database has a transient network blip
 */

(function () {
    'use strict';

    const POLL_INTERVAL_MS = 30000; // 30 seconds
    const LIVE_METRICS_URL = '/dashboard/api/live-metrics/';

    let lastKnownLeadId = null;
    let pollTimer = null;
    let isPolling = false;

    function isUserBusy() {
        // 1. Tab is hidden/minimized
        if (document.hidden) return true;

        // 2. Active input focus
        const activeEl = document.activeElement;
        if (activeEl && (activeEl.tagName === 'INPUT' || activeEl.tagName === 'TEXTAREA' || activeEl.tagName === 'SELECT')) {
            return true;
        }

        // 3. Any bootstrap modal open
        if (document.querySelector('.modal.show')) {
            return true;
        }

        return false;
    }

    function updateMetricElement(key, newValue) {
        if (newValue === undefined || newValue === null) return;

        // Elements tagged with data-live-metric="key"
        const targets = document.querySelectorAll(`[data-live-metric="${key}"]`);
        targets.forEach(el => {
            const currentVal = parseInt(el.textContent.replace(/[^0-9]/g, ''), 10);
            if (!isNaN(currentVal) && currentVal !== newValue) {
                el.textContent = Number(newValue).toLocaleString();
                // Smooth highlight pulse animation
                el.classList.add('live-metric-updated');
                setTimeout(() => el.classList.remove('live-metric-updated'), 1500);
            }
        });
    }

    function showLeadToast(name) {
        // Subtle floating toast notification for new inbound lead
        let container = document.getElementById('crmLiveToastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'crmLiveToastContainer';
            container.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:99999;display:flex;flex-direction:column;gap:8px;pointer-events:none;';
            document.body.appendChild(container);
        }

        const toast = document.createElement('div');
        toast.className = 'shadow-lg border rounded-3 p-3 bg-white d-flex align-items-center gap-3 animate__animated animate__fadeInUp';
        toast.style.cssText = 'min-width:280px;border-left:4px solid #4f46e5 !important;pointer-events:auto;transition:all 0.3s ease;';
        toast.innerHTML = `
            <div class="rounded-circle bg-primary bg-opacity-10 text-primary p-2 d-flex align-items-center justify-content-center" style="width:36px;height:36px;">
                <i class="fa-solid fa-user-plus"></i>
            </div>
            <div class="flex-grow-1">
                <div class="fw-bold small text-dark">New Lead Received</div>
                <div class="text-muted" style="font-size:0.75rem;">${name || 'Inbound Patient'}</div>
            </div>
            <button type="button" class="btn-close btn-close-sm" style="font-size:0.65rem;" onclick="this.parentElement.remove()"></button>
        `;
        container.appendChild(toast);

        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(10px)';
            setTimeout(() => toast.remove(), 400);
        }, 6000);
    }

    async function pollLiveMetrics() {
        if (isPolling) return;
        if (isUserBusy()) return;

        isPolling = true;
        try {
            const urlParams = new URLSearchParams(window.location.search);
            const timeFilter = urlParams.get('time_filter') || 'today';
            const fetchUrl = `${LIVE_METRICS_URL}?time_filter=${encodeURIComponent(timeFilter)}`;

            const res = await fetch(fetchUrl, {
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json'
                }
            });

            if (!res.ok) {
                isPolling = false;
                return;
            }

            const data = await res.json();
            if (data.status === 'success' && data.metrics) {
                // Update metrics on screen
                Object.keys(data.metrics).forEach(k => {
                    updateMetricElement(k, data.metrics[k]);
                });

                // Detect new lead entry
                if (data.latest_lead_id) {
                    if (lastKnownLeadId !== null && data.latest_lead_id > lastKnownLeadId) {
                        showLeadToast(data.latest_lead_name);
                    }
                    lastKnownLeadId = data.latest_lead_id;
                }
            }
        } catch (err) {
            // Silently ignore transient network blips
        } finally {
            isPolling = false;
        }
    }

    // Initialize
    function init() {
        // Add minimal CSS for pulse animation
        const style = document.createElement('style');
        style.textContent = `
            .live-metric-updated {
                animation: metricPulse 1.2s ease-in-out;
            }
            @keyframes metricPulse {
                0% { transform: scale(1); color: inherit; }
                50% { transform: scale(1.15); color: #4f46e5; text-shadow: 0 0 8px rgba(79, 70, 229, 0.4); }
                100% { transform: scale(1); color: inherit; }
            }
        `;
        document.head.appendChild(style);

        // First poll after 5s initial page settling
        setTimeout(pollLiveMetrics, 5000);

        // Recurring interval
        pollTimer = setInterval(pollLiveMetrics, POLL_INTERVAL_MS);

        // Instant poll when user returns to tab
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                setTimeout(pollLiveMetrics, 1000);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

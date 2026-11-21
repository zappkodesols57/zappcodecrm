/**
 * ZappCode CRM - District / City Searchable Dropdown Engine
 * Provides instant client-side autocomplete with at most 10 visible options,
 * scrollable / keyboard navigable, plus "Other" option.
 */

(function (window, document) {
  'use strict';

  let cachedDistricts = [];

  function fetchDistricts(callback) {
    if (cachedDistricts && cachedDistricts.length > 0) {
      if (callback) callback(cachedDistricts);
      return;
    }

    fetch('/static/data/india_districts.json')
      .then(res => res.json())
      .then(data => {
        cachedDistricts = data.districts || [];
        if (!cachedDistricts.includes('Other')) {
          cachedDistricts.push('Other');
        }
        if (callback) callback(cachedDistricts);
      })
      .catch(err => {
        console.warn('Could not load india_districts.json, using fallback:', err);
        cachedDistricts = [
          "Nagpur", "Pune", "Mumbai", "Thane", "Nashik", "Chhatrapati Sambhajinagar (Aurangabad)",
          "Amravati", "Akola", "Bhopal", "Indore", "Jabalpur", "Raipur", "Surat", "Ahmedabad",
          "Bengaluru", "Hyderabad", "Delhi", "Lucknow", "Patna", "Kolkata", "Jaipur", "Other"
        ];
        if (callback) callback(cachedDistricts);
      });
  }

  function attachDistrictSearchDropdown(inputEl) {
    if (!inputEl || inputEl.dataset.districtAttached) return;
    inputEl.dataset.districtAttached = "true";
    inputEl.setAttribute('autocomplete', 'off');

    // Create wrapper & dropdown container
    const wrap = document.createElement('div');
    wrap.className = 'district-search-wrapper position-relative';
    wrap.style.display = inputEl.style.display || 'block';
    wrap.style.width = '100%';

    inputEl.parentNode.insertBefore(wrap, inputEl);
    wrap.appendChild(inputEl);

    const dropdownList = document.createElement('div');
    dropdownList.className = 'district-dropdown-menu shadow-lg border rounded-3 bg-white py-1';
    dropdownList.style.position = 'absolute';
    dropdownList.style.top = '100%';
    dropdownList.style.left = '0';
    dropdownList.style.width = '100%';
    dropdownList.style.zIndex = '1060';
    dropdownList.style.display = 'none';
    dropdownList.style.maxHeight = '320px';
    dropdownList.style.overflowY = 'auto';
    dropdownList.style.marginTop = '2px';
    dropdownList.style.fontSize = '0.85rem';
    wrap.appendChild(dropdownList);

    let activeIndex = -1;

    function renderOptions(query) {
      const q = (query || '').trim().toLowerCase();
      dropdownList.innerHTML = '';
      activeIndex = -1;

      let filtered = [];
      if (!q) {
        // Show first 9 districts + 'Other'
        const topList = cachedDistricts.filter(d => d !== 'Other').slice(0, 9);
        filtered = [...topList, 'Other'];
      } else {
        // Filter by query
        const matches = cachedDistricts.filter(d => d !== 'Other' && d.toLowerCase().includes(q));
        // Take at most 9 matches
        const topMatches = matches.slice(0, 9);
        // Always include 'Other' as 10th option
        filtered = [...topMatches, 'Other'];
      }

      if (filtered.length === 0) {
        filtered = ['Other'];
      }

      filtered.slice(0, 10).forEach((district, idx) => {
        const item = document.createElement('div');
        item.className = 'district-item px-3 py-2 cursor-pointer d-flex align-items-center justify-content-between';
        item.style.cursor = 'pointer';
        item.style.transition = 'background 0.15s ease';

        if (district === 'Other') {
          item.innerHTML = `<span class="fw-semibold text-primary"><i class="fa-solid fa-pen-to-square me-2"></i>Other (Specify city / district)</span>`;
        } else {
          // Highlight match
          if (q) {
            const regex = new RegExp(`(${q})`, 'gi');
            const highlighted = district.replace(regex, '<strong class="text-primary">$1</strong>');
            item.innerHTML = `<span><i class="fa-solid fa-location-dot me-2 text-muted" style="font-size: 0.75rem;"></i>${highlighted}</span>`;
          } else {
            item.innerHTML = `<span><i class="fa-solid fa-location-dot me-2 text-muted" style="font-size: 0.75rem;"></i>${district}</span>`;
          }
        }

        item.addEventListener('mouseenter', () => {
          highlightItem(idx);
        });

        item.addEventListener('mousedown', (e) => {
          e.preventDefault(); // prevent blur
          selectOption(district);
        });

        dropdownList.appendChild(item);
      });

      dropdownList.style.display = 'block';
    }

    function highlightItem(idx) {
      const items = dropdownList.querySelectorAll('.district-item');
      items.forEach((it, i) => {
        if (i === idx) {
          it.style.backgroundColor = '#f1f5f9';
          it.classList.add('active');
        } else {
          it.style.backgroundColor = 'transparent';
          it.classList.remove('active');
        }
      });
      activeIndex = idx;
    }

    function selectOption(district) {
      if (district === 'Other') {
        inputEl.value = 'Other';
        dropdownList.style.display = 'none';
        
        // Show dedicated custom district input right below
        let otherWrap = wrap.querySelector('.other-custom-district-wrap');
        if (!otherWrap) {
          otherWrap = document.createElement('div');
          otherWrap.className = 'other-custom-district-wrap mt-1.5';
          otherWrap.innerHTML = `
            <div class="input-group input-group-sm">
              <span class="input-group-text bg-light text-primary border-end-0 py-1" style="font-size: 0.78rem;">
                <i class="fa-solid fa-pen-to-square"></i>
              </span>
              <input type="text" class="form-control form-control-sm border-start-0 ps-1 other-custom-input" 
                placeholder="Enter custom city / district name..." style="font-size: 0.84rem; height: 32px;" required>
            </div>
          `;
          wrap.appendChild(otherWrap);

          const customInput = otherWrap.querySelector('.other-custom-input');
          customInput.addEventListener('input', function() {
            inputEl.value = this.value.trim() ? this.value.trim() : 'Other';
          });
          customInput.focus();
        } else {
          otherWrap.style.display = 'block';
          const customInput = otherWrap.querySelector('.other-custom-input');
          if (customInput) customInput.focus();
        }
      } else {
        inputEl.value = district;
        dropdownList.style.display = 'none';
        const otherWrap = wrap.querySelector('.other-custom-district-wrap');
        if (otherWrap) otherWrap.style.display = 'none';
      }
      inputEl.dispatchEvent(new Event('change', { bubbles: true }));
      inputEl.dispatchEvent(new Event('input', { bubbles: true }));
    }

    inputEl.addEventListener('focus', () => {
      // If user typed custom name, don't reopen immediately if already filled
      fetchDistricts(() => {
        renderOptions(inputEl.value === 'Other' ? '' : inputEl.value);
      });
    });

    inputEl.addEventListener('input', () => {
      const otherWrap = wrap.querySelector('.other-custom-district-wrap');
      if (otherWrap && inputEl.value !== 'Other') {
        otherWrap.style.display = 'none';
      }
      fetchDistricts(() => {
        renderOptions(inputEl.value);
      });
    });

    inputEl.addEventListener('keydown', (e) => {
      const items = dropdownList.querySelectorAll('.district-item');
      if (dropdownList.style.display === 'block' && items.length > 0) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          activeIndex = (activeIndex + 1) % items.length;
          highlightItem(activeIndex);
          items[activeIndex].scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          activeIndex = (activeIndex - 1 + items.length) % items.length;
          highlightItem(activeIndex);
          items[activeIndex].scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'Enter') {
          if (activeIndex >= 0 && activeIndex < items.length) {
            e.preventDefault();
            items[activeIndex].dispatchEvent(new MouseEvent('mousedown'));
          }
        } else if (e.key === 'Escape') {
          dropdownList.style.display = 'none';
        }
      }
    });

    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) {
        dropdownList.style.display = 'none';
      }
    });
  }

  function initDistrictInputs() {
    // 1. Direct inputs with district classes or named city/location
    document.querySelectorAll('.district-search-input, input[name="city"], input[name="location"]').forEach(input => {
      attachDistrictSearchDropdown(input);
    });

    // 2. Auto-convert native SELECT elements (like Location in Hospital lead form & City in Academy)
    // Matches select[name="location"], select[name="city"], select#id_location, select#id_city
    document.querySelectorAll('select[name="location"], select[name="city"], select#id_location, select#id_city, select.district-search-select').forEach(select => {
      if (select.dataset.districtAttached) return;
      select.dataset.districtAttached = "true";

      const currentVal = select.value || (select.options[select.selectedIndex] ? select.options[select.selectedIndex].value : '');
      const input = document.createElement('input');
      input.type = 'text';
      input.name = select.name;
      input.id = select.id;
      input.className = (select.className || 'form-control').replace('no-tom-select', '').trim() + ' district-search-input';
      input.placeholder = 'Search district / city...';
      input.value = currentVal;
      input.style.cssText = select.style.cssText;

      select.parentNode.insertBefore(input, select);
      select.remove();

      attachDistrictSearchDropdown(input);
    });
  }

  window.ZappCodeDistrict = {
    attach: attachDistrictSearchDropdown,
    init: initDistrictInputs,
    fetch: fetchDistricts
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDistrictInputs);
  } else {
    initDistrictInputs();
  }

})(window, document);

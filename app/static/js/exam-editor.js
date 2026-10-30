/* Exam editor: tabs + question builder serialised to JSON on submit. */
(function () {
  const $ = (s) => document.querySelector(s);
  const esc = App.escapeHtml;
  const TYPES = { mcq: 'Single choice', multi: 'Multiple choice', truefalse: 'True / False', short: 'Short answer', essay: 'Essay' };
  let questions = (window.QUESTIONS_INIT || []).map((q) => ({ ...q, options: q.options || [] }));

  // tabs
  document.querySelectorAll('#tabs [data-tab]').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  function showTab(name) {
    document.querySelectorAll('#tabs [data-tab]').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('[data-tabpanel]').forEach((p) => { p.hidden = p.dataset.tabpanel !== name; });
    document.querySelectorAll('[data-tabpanel-extra]').forEach((p) => { p.hidden = p.dataset.tabpanelExtra !== name; });
    try { sessionStorage.setItem('exam-tab', name); } catch (e) { /* ignore */ }
  }
  try { const t = sessionStorage.getItem('exam-tab'); if (t) showTab(t); } catch (e) { /* ignore */ }

  function blank(type) {
    if (type === 'mcq') return { type, text: '', options: ['', '', '', ''], answer: 0, marks: 1 };
    if (type === 'multi') return { type, text: '', options: ['', '', '', ''], answer: [], marks: 2 };
    if (type === 'truefalse') return { type, text: '', options: ['True', 'False'], answer: true, marks: 1 };
    if (type === 'short') return { type, text: '', options: [], answer: [''], marks: 1 };
    return { type: 'essay', text: '', options: [], answer: null, marks: 5 };
  }

  function render() {
    const list = $('#q-list');
    list.innerHTML = '';
    questions.forEach((q, i) => list.appendChild(card(q, i)));
    $('#q-empty').hidden = questions.length > 0;
    $('#q-count').textContent = questions.length;
    $('#total-marks').textContent = questions.reduce((a, q) => a + (Number(q.marks) || 0), 0);
    App.icons();
  }

  function card(q, i) {
    const el = document.createElement('div');
    el.className = 'q-editor';
    el.innerHTML = `
      <div class="qe-head">
        <span class="qe-num">Q${i + 1}</span>
        <span class="badge badge-indigo">${TYPES[q.type]}</span>
        <span style="flex:1"></span>
        <label class="small muted row" style="gap:6px">Marks <input class="input input-sm" type="number" min="0" step="0.5" style="width:76px" data-f="marks" value="${esc(q.marks)}"></label>
        <button type="button" class="btn btn-sm btn-ghost icon-btn" data-act="up" title="Move up" ${i === 0 ? 'disabled' : ''}><i data-lucide="arrow-up"></i></button>
        <button type="button" class="btn btn-sm btn-ghost icon-btn" data-act="down" title="Move down" ${i === questions.length - 1 ? 'disabled' : ''}><i data-lucide="arrow-down"></i></button>
        <button type="button" class="btn btn-sm btn-ghost icon-btn" data-act="dup" title="Duplicate"><i data-lucide="copy"></i></button>
        <button type="button" class="btn btn-sm btn-ghost icon-btn" data-act="del" title="Delete"><i data-lucide="trash-2"></i></button>
      </div>
      <div class="qe-body">
        <textarea class="input" rows="2" data-f="text" placeholder="Question text"></textarea>
        <div class="mt-1" data-answers></div>
      </div>`;
    el.querySelector('[data-f="text"]').value = q.text;
    const box = el.querySelector('[data-answers]');
    if (q.type === 'mcq' || q.type === 'multi') {
      q.options.forEach((o, j) => {
        const row = document.createElement('div');
        row.className = 'opt-edit';
        const checked = q.type === 'mcq' ? q.answer === j : (q.answer || []).includes(j);
        row.innerHTML = `<input type="${q.type === 'mcq' ? 'radio' : 'checkbox'}" name="ans-${i}" ${checked ? 'checked' : ''} title="Correct answer">
          <input class="input input-sm" placeholder="Option ${String.fromCharCode(65 + j)}">
          <button type="button" class="btn btn-sm btn-ghost icon-btn" title="Remove option"><i data-lucide="x"></i></button>`;
        row.querySelector('.input').value = o;
        row.querySelector('.input').addEventListener('input', (e) => { q.options[j] = e.target.value; });
        row.querySelector('input[name]').addEventListener('change', (e) => {
          if (q.type === 'mcq') q.answer = j;
          else { const s = new Set(q.answer || []); e.target.checked ? s.add(j) : s.delete(j); q.answer = [...s].sort(); }
        });
        row.querySelector('button').addEventListener('click', () => {
          q.options.splice(j, 1);
          if (q.type === 'mcq') q.answer = q.answer === j ? 0 : q.answer > j ? q.answer - 1 : q.answer;
          else q.answer = (q.answer || []).filter((x) => x !== j).map((x) => (x > j ? x - 1 : x));
          render();
        });
        box.appendChild(row);
      });
      const add = document.createElement('button');
      add.type = 'button'; add.className = 'btn btn-sm'; add.innerHTML = '<i data-lucide="plus"></i> Add option';
      add.onclick = () => { q.options.push(''); render(); };
      box.appendChild(add);
      box.insertAdjacentHTML('beforeend', `<span class="small muted" style="margin-left:10px">Tick the correct ${q.type === 'mcq' ? 'option' : 'options'}.</span>`);
    } else if (q.type === 'truefalse') {
      box.innerHTML = `<div class="row"><label class="row small"><input type="radio" name="tf-${i}" ${q.answer ? 'checked' : ''} value="1"> True is correct</label><label class="row small"><input type="radio" name="tf-${i}" ${!q.answer ? 'checked' : ''} value="0"> False is correct</label></div>`;
      box.querySelectorAll('input').forEach((r) => r.addEventListener('change', () => { q.answer = r.value === '1'; }));
    } else if (q.type === 'short') {
      box.innerHTML = '<label class="label">Accepted answers (one per line; matching ignores case and extra spaces)</label><textarea class="input mt-1" rows="2"></textarea>';
      const ta = box.querySelector('textarea');
      ta.value = (q.answer || []).join('\n');
      ta.addEventListener('input', () => { q.answer = ta.value.split('\n').map((s) => s.trim()).filter(Boolean); });
    } else {
      box.innerHTML = '<p class="small muted mb-0">Graded manually by a proctor. In written mode every question is answered on paper.</p>';
    }
    el.querySelector('[data-f="text"]').addEventListener('input', (e) => { q.text = e.target.value; });
    el.querySelector('[data-f="marks"]').addEventListener('input', (e) => { q.marks = Number(e.target.value); $('#total-marks').textContent = questions.reduce((a, x) => a + (Number(x.marks) || 0), 0); });
    el.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => {
      const act = b.dataset.act;
      if (act === 'del') { App.modal({ title: 'Delete this question?', danger: true, icon: 'trash-2', confirmText: 'Delete' }).then((ok) => { if (ok) { questions.splice(i, 1); render(); } }); return; }
      if (act === 'dup') questions.splice(i + 1, 0, { ...JSON.parse(JSON.stringify(q)), id: null });
      if (act === 'up' && i > 0) [questions[i - 1], questions[i]] = [questions[i], questions[i - 1]];
      if (act === 'down' && i < questions.length - 1) [questions[i + 1], questions[i]] = [questions[i], questions[i + 1]];
      render();
    }));
    return el;
  }

  document.querySelectorAll('[data-add]').forEach((b) => b.addEventListener('click', () => {
    questions.push(blank(b.dataset.add));
    render();
    const items = document.querySelectorAll('.q-editor');
    items[items.length - 1]?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    items[items.length - 1]?.querySelector('textarea')?.focus();
  }));

  $('#exam-form').addEventListener('submit', () => {
    $('#questions_json').value = JSON.stringify(questions.map((q) => ({ ...q, options: (q.options || []) })));
  });

  render();
})();

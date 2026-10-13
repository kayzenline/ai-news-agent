(() => {
  "use strict";
  const notice = document.getElementById("run-status");
  const connection = document.getElementById("run-connection");
  const runsBody = document.getElementById("runs-body");
  const briefsBody = document.getElementById("briefs-body");
  const form = document.getElementById("generate-form");
  const button = form.querySelector("button");
  const active = (run) => run && ["queued", "running"].includes(run.status);
  const labels = {queued: "排队中", running: "运行中", succeeded: "成功", no_news: "无新增内容",
                  failed: "失败", interrupted: "已中断"};
  let focusId = notice.dataset.runId;
  let timer;
  let polling = false;
  let submitting = false;
  let awaitingConfirmation = false;
  let lastRunId = runsBody.querySelector("[data-run-id]")?.dataset.runId;
  let submissionPreviousId;
  let noticeKey = "";
  let revision = 0;
  let hasActiveRun = Boolean(runsBody.querySelector('[data-status="queued"], [data-status="running"]'));

  function cell(value, className = "") {
    const td = document.createElement("td");
    td.textContent = value ?? "";
    td.className = className;
    return td;
  }

  function briefLink(id, text) {
    const link = document.createElement("a");
    link.href = `/briefs/${id}`;
    link.textContent = text;
    return link;
  }

  function showRun(run) {
    if (!run) return;
    const key = JSON.stringify([run.run_id, run.status, run.error, run.brief_id]);
    if (key === noticeKey) return;
    noticeKey = key;
    focusId = run.run_id;
    const messages = {queued: "⏳ 已受理，正在排队。", running: "⏳ Agent 正在生成简报，完成后会自动显示结果。",
      succeeded: "✅ 简报生成成功。", no_news: "ℹ️ 本次运行结束，没有新增内容。",
      failed: "❌ 本次运行失败。", interrupted: "⚠️ 本次运行已中断。"};
    notice.className = run.status;
    if (run.status === "succeeded" && run.error === "max_steps") {
      notice.textContent = "⚠️ 简报已生成（达到最大步数上限 max_steps，已输出阶段草稿）。";
    } else if (run.status === "no_news" && run.error === "max_steps") {
      notice.textContent = "ℹ️ 本次运行结束，没有新增内容（达到最大步数上限 max_steps）。";
    } else {
      notice.textContent = messages[run.status] || `当前状态：${run.status}`;
      if (run.error) notice.append(document.createTextNode(` 原因：${run.error}`));
    }
    if (run.brief_id) {
      notice.append(document.createTextNode(" "), briefLink(run.brief_id,
        run.status === "failed" ? "查看运行记录与 trace" : "查看简报"));
    }
  }

  function applyActivity(data) {
    const rows = data.runs.map((run) => {
      const row = document.createElement("tr");
      row.dataset.runId = run.run_id;
      row.dataset.status = run.status;
      const reason = cell(run.error);
      if (run.brief_id) reason.append(document.createTextNode(" "), briefLink(run.brief_id, "查看"));
      row.append(cell(run.run_id), cell(run.trigger), cell(labels[run.status] || run.status, run.status),
                 reason, cell(run.created_at));
      return row;
    });
    if (!rows.length) {
      const row = document.createElement("tr");
      const empty = cell("暂无");
      empty.colSpan = 5;
      row.append(empty);
      rows.push(row);
    }
    runsBody.replaceChildren(...rows);

    // 只补充/更新最近简报，保留初始页面中更早的历史与未保存的偏好输入。
    if (data.briefs.length) briefsBody.querySelector('[data-empty="true"]')?.remove();
    for (const brief of [...data.briefs].reverse()) {
      const row = document.createElement("tr");
      row.dataset.briefId = String(brief.id);
      const linkCell = cell("");
      linkCell.append(briefLink(brief.id, "查看 + trace"));
      row.append(cell(brief.date), cell(`${brief.status} (${brief.stopped_reason})`, brief.status),
                 cell(brief.steps), cell(`${brief.tokens_in}/${brief.tokens_out}`), linkCell);
      const old = [...briefsBody.children].find((entry) => entry.dataset.briefId === String(brief.id));
      if (old) old.replaceWith(row);
      else briefsBody.prepend(row);
    }
    hasActiveRun = data.runs.some(active);
    lastRunId = data.runs[0]?.run_id;
    let selected = data.runs.find((run) => run.run_id === focusId);
    if (!selected || (!active(selected) && data.runs[0]?.run_id !== focusId)) selected = data.runs[0];
    showRun(selected);
    button.disabled = submitting || awaitingConfirmation || hasActiveRun;
    button.textContent = hasActiveRun ? "⏳ 正在生成…" : "▶ 立即生成今日简报";
  }

  async function request(url, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, {cache: "no-store", ...options, signal: controller.signal});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } finally {
      clearTimeout(timeout);
    }
  }

  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(poll, delay);
  }

  async function poll() {
    if (polling) return;
    polling = true;
    const version = revision;
    let delay = hasActiveRun && !document.hidden ? 2000 : 10000;
    try {
      const data = await request(`/users/${notice.dataset.userId}/activity`);
      if (version === revision && !submitting) {
        const unconfirmed = awaitingConfirmation;
        awaitingConfirmation = false;
        applyActivity(data);
        if (unconfirmed && !hasActiveRun && lastRunId === submissionPreviousId) {
          noticeKey = "";
          notice.className = "";
          notice.textContent = "暂未查到此次生成请求的运行记录，可以再次尝试。";
        }
        connection.hidden = true;
      }
      delay = hasActiveRun && !document.hidden ? 2000 : 10000;
    } catch {
      if (version === revision && !submitting) {
        connection.textContent = "暂时无法更新状态，正在自动重连；任务结果尚未确认。";
        connection.hidden = false;
      }
      delay = 10000;
    } finally {
      polling = false;
      schedule(delay);
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || awaitingConfirmation || hasActiveRun) return;
    submitting = true;
    submissionPreviousId = lastRunId;
    revision += 1;
    button.disabled = true;
    noticeKey = "";
    notice.textContent = "⏳ 正在保存偏好并提交生成请求…";
    try {
      const prefsForm = document.getElementById("prefs-form") || document.querySelector('form[action$="/prefs"]');
      if (prefsForm) {
        await fetch(prefsForm.action, {method: "POST", body: new FormData(prefsForm)});
      }
      const run = await request(form.action, {method: "POST", headers: {Accept: "application/json"}});
      hasActiveRun = active(run);
      showRun(run);
      connection.hidden = true;
    } catch {
      awaitingConfirmation = true;
      connection.textContent = "提交结果暂时无法确认，正在查询运行记录，请勿重复提交。";
      connection.hidden = false;
    } finally {
      revision += 1;
      submitting = false;
      button.disabled = awaitingConfirmation || hasActiveRun;
      schedule(0);
    }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(0); });
  button.disabled = hasActiveRun;
  poll();
})();

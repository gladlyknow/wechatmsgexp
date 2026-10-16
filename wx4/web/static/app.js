"use strict";

const params = new URLSearchParams(location.search);
const suppliedToken = params.get("token");
if (suppliedToken) {
  sessionStorage.setItem("wxArchiveToken", suppliedToken);
  history.replaceState({}, "", location.pathname);
}

const state = {
  token: suppliedToken || sessionStorage.getItem("wxArchiveToken") || "",
  accounts: [], account: null, session: null, sessions: [],
  messages: [], oldest: 0, kind: "", view: "chat", loading: false,
  dateStart: 0, dateEnd: 0, directory: [], directoryTotal: 0,
  directoryCategory: "contacts", directoryQuery: "", media: [],
  mediaTotal: 0, mediaCategory: "all",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const node = (tag, className, text) => {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = text;
  return item;
};

function toast(message) {
  const item = $("#toast");
  item.textContent = message;
  item.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => item.classList.remove("show"), 2600);
}

async function api(path, query = {}) {
  const url = new URL(path, location.origin);
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
  });
  const response = await fetch(url, {headers: {"X-Archive-Token": state.token}});
  if (response.status === 401) throw new Error("访问令牌无效，请用启动时显示的完整地址打开。");
  if (!response.ok) throw new Error(`请求失败 (${response.status})`);
  return response.json();
}

async function apiPost(path, payload) {
  const response = await fetch(new URL(path, location.origin), {
    method:"POST", headers:{"X-Archive-Token":state.token,"Content-Type":"application/json"},
    body:JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) throw new Error("访问令牌无效，请用启动时显示的完整地址打开。");
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function mediaUrl(path) {
  const url = new URL(path, location.origin);
  url.searchParams.set("token", state.token);
  return url.toString();
}

function initials(value) {
  const clean = (value || "忆").replace(/@chatroom$/, "").trim();
  return [...clean].slice(0, 2).join("") || "忆";
}

function fmtTime(value, withDate = false) {
  if (!value) return "";
  const date = new Date(value * 1000);
  const options = withDate
    ? {year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"}
    : {hour:"2-digit", minute:"2-digit"};
  return new Intl.DateTimeFormat("zh-CN", options).format(date).replaceAll("/", "-");
}

function fmtDay(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {year:"numeric", month:"long", day:"numeric"})
    .format(new Date(value * 1000));
}

function fmtNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function kindLabel(kind) {
  return ({text:"文本", image:"图片", voice:"语音", video:"视频", link:"链接",
    file:"文件", emoji:"表情", quote:"引用", system:"系统", appmsg:"应用"})[kind] || kind || "其他";
}

async function bootstrap() {
  try {
    const setup = await api("/api/setup/status");
    state.setup = setup; renderSetup(setup);
    if (!setup.archive_ready) {
      switchView("setup");
      return;
    }
    const [accounts, overview] = await Promise.all([api("/api/accounts"), api("/api/overview")]);
    state.accounts = accounts;
    renderAccounts();
    renderOverview(overview);
    if (accounts.length) {
      state.account = accounts[0];
      $("#accountSelect").value = String(state.account.id);
      await loadSessions();
    } else {
      toast("档案库中还没有账号，请先运行 archive。");
    }
  } catch (error) {
    toast(error.message);
    $("#emptyState h2").textContent = "无法读取档案";
    $("#emptyState p").textContent = error.message;
  }
}

function renderAccounts() {
  const select = $("#accountSelect");
  select.replaceChildren();
  state.accounts.forEach((account) => {
    const option = node("option", "", account.display_name || account.wxid);
    option.value = account.id;
    option.title = account.wxid;
    select.append(option);
  });
}

async function loadSessions(query = "") {
  if (!state.account) return;
  state.sessions = await api("/api/sessions", {account: state.account.id, q: query, limit: 300});
  $("#listTitle").textContent = query ? "会话匹配" : "最近会话";
  $("#sessionCount").textContent = fmtNumber(state.sessions.length);
  renderSessions();
  renderExportSessions();
}

function renderSessions() {
  const list = $("#sessionList");
  list.replaceChildren();
  state.sessions.forEach((session) => {
    const button = node("button", "session-item");
    button.type = "button";
    if (state.session?.username === session.username) button.classList.add("active");
    const avatar = node("span", `avatar${session.is_group ? " group" : ""}`, initials(session.display_name));
    const copy = node("span", "session-copy");
    const row = node("span", "session-row");
    row.append(node("span", "session-name", session.display_name || session.username));
    copy.append(row, node("span", "session-summary", session.summary || `${session.message_count} 条消息`));
    button.append(avatar, copy, node("time", "session-time", fmtTime(session.sort_time)));
    button.addEventListener("click", () => selectSession(session));
    list.append(button);
  });
  if (!state.sessions.length) list.append(node("div", "empty-state", "没有找到会话"));
}

async function selectSession(session) {
  state.session = session;
  state.messages = [];
  state.oldest = 0;
  clearTimeFilter(false);
  switchView("chat");
  renderSessions();
  $("#conversationTitle").textContent = session.display_name || session.username;
  $("#conversationMeta").textContent = `${fmtNumber(session.message_count)} 条消息 · ${session.is_group ? "群聊" : "单聊"} · ${session.username}`;
  $("#headAvatar").textContent = initials(session.display_name);
  $("#emptyState").classList.add("hidden");
  $("#overviewView").classList.add("hidden");
  $("#messageViewport").classList.remove("hidden");
  document.querySelector(".app-shell").classList.add("chat-open");
  await Promise.all([loadMessages(false), loadDetail(), loadTimeline()]);
}

async function loadMessages(earlier) {
  if (!state.session || state.loading) return;
  state.loading = true;
  const viewport = $("#messageViewport");
  const previousHeight = viewport.scrollHeight;
  try {
    const rows = await api("/api/messages", {
      account: state.account.id, session: state.session.username,
      before: earlier ? state.oldest : 0, kinds: state.kind, limit: 80,
      start: state.dateStart, end: state.dateEnd,
    });
    state.messages = earlier ? [...rows, ...state.messages] : rows;
    state.oldest = state.messages.length ? state.messages[0].create_time : 0;
    renderMessages();
    $("#loadEarlier").classList.toggle("hidden", rows.length < 80 || Boolean(state.dateStart));
    if (earlier) viewport.scrollTop = viewport.scrollHeight - previousHeight;
    else viewport.scrollTop = viewport.scrollHeight;
  } catch (error) { toast(error.message); }
  finally { state.loading = false; }
}

async function loadTimeline() {
  const select = $("#monthJump");
  select.replaceChildren();
  const all = node("option", "", "全部月份"); all.value = ""; select.append(all);
  if (!state.session) return;
  try {
    const data = await api("/api/timeline", {account:state.account.id, session:state.session.username});
    data.months.forEach((item) => {
      const option = node("option", "", `${item.month} · ${fmtNumber(item.count)} 条`);
      option.value = item.month; select.append(option);
    });
  } catch (error) { toast(error.message); }
}

function localDateValue(timestamp) {
  const date = new Date(timestamp * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}`;
}

function clearTimeFilter(reload = true) {
  state.dateStart = 0; state.dateEnd = 0;
  $("#monthJump").value = ""; $("#dateJump").value = "";
  $("#clearDate").classList.add("hidden");
  if (reload && state.session) { state.messages=[]; state.oldest=0; loadMessages(false); }
}

function setTimeRange(start, end) {
  state.dateStart = Math.floor(start.getTime()/1000);
  state.dateEnd = Math.floor(end.getTime()/1000);
  state.messages=[]; state.oldest=0;
  $("#clearDate").classList.remove("hidden");
  loadMessages(false);
}

function attachment(message, kind) {
  return message.attachments.find((item) => item.kind === kind);
}

function allAttachments(message, kind) {
  return message.attachments.filter((item) => item.kind === kind);
}

function safeExternalUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url : null;
  } catch (_) { return null; }
}

function renderContent(message) {
  const fragment = document.createDocumentFragment();
  const originalImage = attachment(message, "image");
  const thumb = attachment(message, "image_thumbnail") || originalImage;
  const voice = attachment(message, "voice_audio") || attachment(message, "audio");
  const video = attachment(message, "video");
  const poster = attachment(message, "video_poster");
  const emojis = allAttachments(message, "emoji");
  const files = allAttachments(message, "file");

  if (message.kind === "link") {
    const url = safeExternalUrl(message.extra?.url);
    if (url) {
      const link = node("a", "link-card");
      link.href = url.toString(); link.target = "_blank"; link.rel = "noopener noreferrer";
      link.append(node("div", "card-title", message.extra.title || message.text || url.toString()));
      if (message.extra.description) link.append(node("div", "card-meta", message.extra.description));
      link.append(node("div", "link-host", url.host));
      fragment.append(link);
    } else fragment.append(document.createTextNode(message.text || "[链接]"));
  } else if (message.kind === "quote") {
    fragment.append(document.createTextNode(message.extra?.reply_text || message.text || "[引用消息]"));
    const quote = node("div", "quote-block");
    quote.append(node("strong", "", message.extra?.refer_name || message.extra?.refer_from || "原消息"));
    quote.append(document.createTextNode(message.extra?.refer_text || ""));
    fragment.append(quote);
  } else if (!["image", "voice", "video", "file", "emoji"].includes(message.kind)) {
    fragment.append(document.createTextNode(message.text || ""));
  }

  if (originalImage && thumb) {
    const image = node("img", "media-image");
    image.src = mediaUrl(thumb.url); image.alt = "聊天图片"; image.loading = "lazy";
    image.addEventListener("click", () => openPreview(mediaUrl(originalImage.url)));
    fragment.append(image);
  } else if (message.kind === "image") fragment.append(document.createTextNode(message.text || "[图片未下载]"));

  if (voice) {
    const audio = node("audio", "media-audio");
    audio.controls = true; audio.preload = "metadata"; audio.src = mediaUrl(voice.url);
    fragment.append(audio);
  } else if (message.kind === "voice") fragment.append(document.createTextNode(message.text || "[语音未下载]"));

  if (video) {
    const item = node("video", "media-video");
    item.controls = true; item.preload = "metadata"; item.src = mediaUrl(video.url);
    if (poster) item.poster = mediaUrl(poster.url);
    fragment.append(item);
  } else if (message.kind === "video") fragment.append(document.createTextNode(message.text || "[视频未下载]"));

  if (emojis.length) {
    const row = node("div", "emoji-row");
    const tokenCount = Math.max(1, Math.min(12, (message.text.match(/\[[^\]]+\]/g) || []).length));
    for (let index = 0; index < tokenCount; index += 1) {
      const image = node("img", "emoji-image");
      image.src = mediaUrl(emojis[index % emojis.length].url); image.alt = "表情"; image.loading = "lazy";
      row.append(image);
    }
    fragment.append(row);
  } else if (message.kind === "emoji") fragment.append(document.createTextNode(message.text || "[表情未下载]"));

  files.forEach((file) => {
    const link = node("a", "file-card"); link.href = mediaUrl(file.url); link.download = "";
    link.append(node("span", "file-icon", "文"));
    const copy = node("span", "card-copy");
    copy.append(node("span", "card-title", message.extra?.filename || "聊天文件"),
      node("span", "card-meta", `${(file.size / 1024).toFixed(1)} KB`));
    link.append(copy); fragment.append(link);
  });
  return fragment;
}

function renderMessages() {
  const list = $("#messageList");
  list.replaceChildren();
  let day = "";
  state.messages.forEach((message) => {
    const nextDay = fmtDay(message.create_time);
    if (nextDay !== day) {
      const sep = node("div", "date-sep"); sep.append(node("span", "", nextDay)); list.append(sep); day = nextDay;
    }
    if (message.kind === "system") {
      const sys = node("div", "system-message"); sys.append(node("span", "", message.text)); list.append(sys); return;
    }
    const item = node("article", `message${message.is_sender ? " mine" : ""}`);
    item.dataset.id = message.id;
    item.append(node("div", "message-avatar", initials(message.is_sender ? "我" : message.sender_name)));
    const body = node("div", "message-body");
    body.append(node("div", "message-name", message.is_sender ? "我" : message.sender_name));
    const bubble = node("div", "bubble"); bubble.append(renderContent(message));
    body.append(bubble, node("div", "message-time", fmtTime(message.create_time)));
    item.append(body); list.append(item);
  });
  if (!state.messages.length) list.append(node("div", "empty-state", "该筛选条件下没有消息"));
}

async function loadDetail() {
  if (!state.session) return;
  try {
    const detail = await api("/api/session-detail", {account:state.account.id, session:state.session.username});
    const root = $("#detailContent"); root.replaceChildren();
    const avatar = node("div", "detail-avatar", initials(detail.display_name));
    root.append(avatar, node("h2", "", detail.display_name || detail.username),
      node("p", "", detail.username));
    const stats = node("div", "detail-stats");
    [[detail.message_count, "消息"], [detail.members.length, detail.is_group ? "成员" : "参与者"]].forEach(([value,label]) => {
      const card=node("div","detail-stat");card.append(node("b","",fmtNumber(value)),node("span","",label));stats.append(card);
    }); root.append(stats);
    const types=node("section","detail-section");types.append(node("h3","","消息类型"));
    detail.type_counts.forEach((item)=>types.append(node("span","type-tag",`${kindLabel(item.kind)} ${item.count}`)));root.append(types);
    if (detail.members.length) {
      const section=node("section","detail-section");section.append(node("h3","","群成员"));const members=node("div","member-list");
      detail.members.forEach((item)=>members.append(node("span","member",item.display_name)));section.append(members);root.append(section);
    }
  } catch (error) { toast(error.message); }
}

async function runSearch(query) {
  if (!query) { await loadSessions(); return; }
  try {
    const rows = await api("/api/search", {q:query, account:state.account?.id, limit:150});
    $("#listTitle").textContent = `搜索“${query}”`;
    $("#sessionCount").textContent = fmtNumber(rows.length);
    const list=$("#sessionList");list.replaceChildren();
    rows.forEach((item)=>{
      const button=node("button","session-item search-hit");button.type="button";
      const avatar=node("span","avatar",initials(item.sender_name));const copy=node("span","session-copy");
      copy.append(node("span","session-name",item.sender_name || item.session_username),node("span","session-summary",item.snippet || item.text));
      button.append(avatar,copy,node("time","session-time",fmtTime(item.create_time)));
      button.addEventListener("click",async()=>{
        const session=state.sessions.find((value)=>value.username===item.session_username) ||
          {username:item.session_username,display_name:item.session_username,message_count:0,is_group:false};
        await selectSession(session);
        setTimeout(()=>document.querySelector(`[data-id="${item.id}"]`)?.scrollIntoView({block:"center"}),100);
      });list.append(button);
    });
    if(!rows.length)list.append(node("div","empty-state","没有匹配消息"));
  } catch(error){toast(error.message);}
}

function renderOverview(data) {
  const metrics = [["messages","消息"],["sessions","会话"],["contacts","联系人"],["attachments","附件"],["accounts","账号"]];
  const grid=$("#metricGrid");grid.replaceChildren();metrics.forEach(([key,label])=>{
    const card=node("article","metric");card.append(node("b","",fmtNumber(data.counts[key])),node("span","",label));grid.append(card);
  });
  $("#overviewSpan").textContent=data.time_start?`${fmtTime(data.time_start,true)} 至 ${fmtTime(data.time_end,true)}`:"档案中暂无消息";
  const max=Math.max(...data.types.map((item)=>item.count),1);const bars=$("#typeBars");bars.replaceChildren();
  data.types.slice(0,9).forEach((item)=>{const row=node("div","type-bar");row.append(node("span","",kindLabel(item.kind)));
    const track=node("span","bar-track"),fill=node("span","bar-fill");fill.style.width=`${item.count/max*100}%`;track.append(fill);
    row.append(track,node("b","",fmtNumber(item.count)));bars.append(row);});
  const run=data.last_run;$("#runStatus").textContent=run?.status||"无记录";const detail=$("#runDetail");detail.replaceChildren();
  if(run){const report=run.report||{};[["完成时间",run.finished_at||run.started_at],["新增消息",report.inserted||0],["更新消息",report.updated||0],["媒体问题",report.media_issues||0],["增量跳过",report.skipped?"是":"否"]].forEach(([label,value])=>{const row=node("div","run-row");row.append(node("span","",label),node("b","",String(value)));detail.append(row);});}
}

async function loadAnalytics(year = "") {
  if (!state.account) return;
  try {
    const data = await api("/api/analytics", {account:state.account.id, year});
    state.analytics = data; renderAnalytics(data);
  } catch (error) { toast(error.message); }
}

function renderAnalytics(data) {
  const summary=data.summary||{},year=data.year||"全部";
  $("#analyticsTitle").textContent=`${year} 年的微信记忆`;
  $("#analyticsSpan").textContent=summary.first_time?`${fmtDay(summary.first_time)} 至 ${fmtDay(summary.last_time)}`:"这一年还没有消息";
  const select=$("#analyticsYear"),selected=select.value;select.replaceChildren();
  data.years.forEach((value)=>{const option=node("option","",`${value} 年`);option.value=value;select.append(option);});
  select.value=data.year||selected;
  const metrics=$("#analyticsMetrics");metrics.replaceChildren();
  [[summary.messages,"消息"],[summary.active_days,"活跃天数"],[summary.sessions,"会话"],[summary.characters,"文字数"]].forEach(([value,label])=>{const card=node("article","metric");card.append(node("b","",fmtNumber(value)),node("span","",label));metrics.append(card);});
  const story=$("#firstStory");story.replaceChildren();
  if(data.first){story.append(node("div","story-year",new Date(data.first.create_time*1000).getFullYear()),node("div","story-copy"));const copy=story.lastChild;copy.append(node("span","","这一年的第一条记忆"),node("h3","",data.first.session_name),node("p","",`${fmtTime(data.first.create_time,true)} · ${data.first.sender_name || "系统"}`),node("blockquote","",data.first.text || kindLabel("system")));}
  else story.append(node("div","empty-state compact","暂无年度记忆"));
  renderHeatmap(data.daily||[],Number(data.year));
  renderHorizontalChart($("#monthlyChart"),data.monthly||[],"month");
  renderHourly(data.hourly||[]);
  const total=Number(summary.sent||0)+Number(summary.received||0),sent=total?Math.round(summary.sent/total*100):0;
  $("#directionPercent").textContent=`${sent}%`;$("#directionRing").style.background=`conic-gradient(var(--accent) 0 ${sent}%,var(--surface-2) ${sent}% 100%)`;
  const legend=$("#typeLegend");legend.replaceChildren();(data.types||[]).slice(0,7).forEach((item)=>{const row=node("div","legend-row");row.append(node("span","",kindLabel(item.kind)),node("b","",fmtNumber(item.count)));legend.append(row);});
  renderRanks($("#topSessions"),data.top_sessions||[],"display_name",true);
  renderRanks($("#groupSenders"),data.group_senders||[],"sender_name",false);
  const cloud=$("#wordCloud");cloud.replaceChildren();const max=Math.max(...(data.words||[]).map((item)=>item.count),1);(data.words||[]).forEach((item,index)=>{const word=node("span","",item.word);word.style.fontSize=`${11+Math.sqrt(item.count/max)*22}px`;word.style.opacity=String(.58+Math.min(.42,item.count/max));word.style.color=`hsl(${150+(index%6)*9} 45% ${document.documentElement.dataset.theme==="dark"?65:34}%)`;word.title=`${item.count} 次`;cloud.append(word);});if(!(data.words||[]).length)cloud.append(node("div","empty-state compact","文字样本不足"));
}

function renderHeatmap(rows, year) {
  const root=$("#calendarHeatmap");root.replaceChildren();if(!year)return;
  const counts=new Map(rows.map((item)=>[item.day,item.count]));const max=Math.max(...rows.map((item)=>item.count),1);
  const start=new Date(year,0,1),end=new Date(year+1,0,1),offset=(start.getDay()+6)%7;
  for(let index=0;index<offset;index+=1)root.append(node("span","heat-cell empty"));
  for(let date=new Date(start);date<end;date.setDate(date.getDate()+1)){const stamp=Math.floor(date.getTime()/1000),key=localDateValue(stamp),count=counts.get(key)||0;const level=count?Math.max(1,Math.ceil(count/max*4)):0;const cell=node("span",`heat-cell level-${level}`);cell.title=`${key} · ${count} 条`;root.append(cell);}
  $("#heatmapLegend").textContent=`${fmtNumber(rows.length)} 个活跃日`;
}

function renderHorizontalChart(root, rows, key) {
  root.replaceChildren();const max=Math.max(...rows.map((item)=>item.count),1);rows.forEach((item)=>{const row=node("div","horizontal-row");row.append(node("span","",item[key]));const track=node("span","horizontal-track"),fill=node("span","horizontal-fill");fill.style.width=`${item.count/max*100}%`;track.append(fill);row.append(track,node("b","",fmtNumber(item.count)));root.append(row);});if(!rows.length)root.append(node("div","empty-state compact","暂无数据"));
}

function renderHourly(rows) {
  const counts=new Map(rows.map((item)=>[Number(item.hour),item.count])),max=Math.max(...rows.map((item)=>item.count),1),root=$("#hourlyChart");root.replaceChildren();
  for(let hour=0;hour<24;hour+=1){const value=counts.get(hour)||0,item=node("div","hour-column"),bar=node("span","hour-bar");bar.style.height=`${Math.max(value?5:1,value/max*100)}%`;bar.title=`${hour}:00 · ${value} 条`;item.append(bar,node("small","",hour%3===0?String(hour):""));root.append(item);}
}

function renderRanks(root, rows, labelKey, clickable) {
  root.replaceChildren();rows.forEach((item,index)=>{const row=node(clickable?"button":"div","rank-row");if(clickable)row.type="button";row.append(node("span","rank-index",String(index+1)),node("span","rank-name",item[labelKey]||"未知"),node("b","",fmtNumber(item.count)));if(clickable)row.addEventListener("click",()=>selectSession({username:item.session_username,display_name:item.display_name,message_count:item.count,is_group:Boolean(item.is_group)}));root.append(row);});if(!rows.length)root.append(node("div","empty-state compact","暂无数据"));
}

async function loadDirectory(reset = true) {
  if (!state.account) return;
  const offset = reset ? 0 : state.directory.length;
  try {
    const data = await api("/api/directory", {account:state.account.id,
      category:state.directoryCategory, q:state.directoryQuery, offset, limit:100});
    state.directory = reset ? data.items : [...state.directory, ...data.items];
    state.directoryTotal = data.total;
    renderDirectory();
  } catch (error) { toast(error.message); }
}

function renderDirectory() {
  $("#directoryCount").textContent = fmtNumber(state.directoryTotal);
  const grid = $("#directoryGrid"); grid.replaceChildren();
  state.directory.forEach((item) => {
    const card = node("article", "directory-card");
    const avatar = node("div", `directory-avatar${item.is_group ? " group" : ""}`, initials(item.display_name));
    const copy = node("div", "directory-copy");
    copy.append(node("h3", "", item.display_name || item.username),
      node("p", "", item.is_group ? `${fmtNumber(item.member_count)} 位成员` : (item.remark || item.nickname || item.alias || item.username)),
      node("span", "", `${fmtNumber(item.message_count)} 条消息${item.last_time ? ` · ${fmtTime(item.last_time,true)}` : ""}`));
    const open = node("button", "directory-open", item.message_count ? "打开会话" : "暂无会话");
    open.type = "button"; open.disabled = !item.message_count;
    open.addEventListener("click", () => selectSession({username:item.username,
      display_name:item.display_name, message_count:item.message_count, is_group:Boolean(item.is_group)}));
    card.append(avatar, copy, open); grid.append(card);
  });
  if (!state.directory.length) grid.append(node("div", "empty-state", "没有找到匹配项"));
  $("#directoryMore").classList.toggle("hidden", state.directory.length >= state.directoryTotal);
}

async function loadMedia(reset = true) {
  if (!state.account) return;
  const offset = reset ? 0 : state.media.length;
  try {
    const data = await api("/api/media", {account:state.account.id,
      category:state.mediaCategory, offset, limit:60});
    state.media = reset ? data.items : [...state.media, ...data.items];
    state.mediaTotal = data.total;
    renderMedia();
  } catch (error) { toast(error.message); }
}

function renderMedia() {
  $("#mediaCount").textContent = fmtNumber(state.mediaTotal);
  const grid = $("#mediaGrid"); grid.replaceChildren();
  state.media.forEach((item) => {
    const card = node("article", "gallery-card");
    const preview = node("div", "gallery-preview");
    const src = mediaUrl(item.url);
    if (["image", "emoji"].includes(item.kind)) {
      const image = node("img"); image.alt = item.kind === "emoji" ? "表情" : "图片";
      image.src = mediaUrl(item.thumbnail_url || item.url); image.loading = "lazy";
      image.addEventListener("click", () => openPreview(src)); preview.append(image);
    } else if (item.kind === "video") {
      const video = node("video"); video.controls = true; video.preload = "metadata"; video.src = src;
      if (item.poster_url) video.poster = mediaUrl(item.poster_url); preview.append(video);
    } else if (["voice_audio", "audio"].includes(item.kind)) {
      preview.classList.add("audio-preview");
      const audio = node("audio"); audio.controls = true; audio.preload = "metadata"; audio.src = src;
      preview.append(node("div", "audio-mark", "声"), audio);
    } else {
      const link = node("a", "file-preview", "打开文件"); link.href = src; link.download = "";
      preview.append(node("div", "file-mark", "文"), link);
    }
    const meta = node("div", "gallery-meta");
    const heading = node("div", "gallery-heading");
    heading.append(node("b", "", item.session_name || item.session_username),
      node("time", "", fmtTime(item.create_time,true)));
    const jump = node("button", "gallery-jump", "查看消息"); jump.type = "button";
    jump.addEventListener("click", async () => {
      await selectSession({username:item.session_username,
        display_name:item.session_name || item.session_username, message_count:0,
        is_group:item.session_username.endsWith("@chatroom")});
      const value = localDateValue(item.create_time); $("#dateJump").value = value;
      $("#monthJump").value = "";
      const start = new Date(`${value}T00:00:00`), end = new Date(start); end.setDate(end.getDate()+1);
      setTimeRange(start,end);
      setTimeout(() => document.querySelector(`[data-id="${item.message_id}"]`)?.scrollIntoView({block:"center"}), 160);
    });
    meta.append(heading, node("p", "", item.text || kindLabel(item.kind)), jump);
    card.append(preview, meta); grid.append(card);
  });
  if (!state.media.length) grid.append(node("div", "empty-state", "该分类暂无可用媒体"));
  $("#mediaMore").classList.toggle("hidden", state.media.length >= state.mediaTotal);
}

function renderExportSessions() {
  const select = $("#exportSession");
  const selected = select.value; select.replaceChildren();
  const all = node("option", "", "全部有消息会话"); all.value = "*"; select.append(all);
  state.sessions.filter((item) => item.message_count).forEach((item) => {
    const option = node("option", "", `${item.display_name || item.username} · ${fmtNumber(item.message_count)} 条`);
    option.value = item.username; select.append(option);
  });
  if ([...select.options].some((item) => item.value === selected)) select.value = selected;
}

function fmtBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024*1024) return `${(size/1024).toFixed(1)} KB`;
  return `${(size/1024/1024).toFixed(1)} MB`;
}

async function loadExportJobs() {
  clearTimeout(state.exportTimer);
  if (state.view !== "export") return;
  try {
    const jobs = await api("/api/exports"); renderExportJobs(jobs);
    if (jobs.some((item) => ["queued","running"].includes(item.status))) {
      state.exportTimer = setTimeout(loadExportJobs, 900);
    }
  } catch (error) { toast(error.message); }
}

function renderExportJobs(jobs) {
  const root = $("#exportJobs"); root.replaceChildren();
  const labels = {queued:"等待中",running:"进行中",done:"已完成",failed:"失败"};
  const formatLabels={html:"HTML",md:"MD",json:"JSON",csv:"CSV",txt:"TXT",sqlite:"DB",contacts_csv:"联系人",messages_csv:"全消息"};
  jobs.forEach((job) => {
    const item = node("article", `job-item ${job.status}`);
    const icon = node("div", "job-icon", formatLabels[job.format]||job.format.toUpperCase());
    const copy = node("div", "job-copy"); copy.append(node("b", "", job.display_name),
      node("span", "", `${labels[job.status] || job.status} · ${job.message}`));
    const action = node("div", "job-action");
    if (job.status === "done") {
      const amount=job.contact_count?`${fmtNumber(job.contact_count)} 位联系人`:(job.session_count?`${fmtNumber(job.session_count)} 个会话`:`${fmtNumber(job.message_count)} 条`);
      action.append(node("small", "", `${amount} · ${fmtBytes(job.size)}`));
      const link = node("a", "job-download", "下载 ZIP");
      link.href = mediaUrl(`/api/exports/${job.id}/download`); action.append(link);
    } else if (job.status === "running" || job.status === "queued") {
      action.append(node("span", "job-spinner", ""));
    }
    item.append(icon, copy, action); root.append(item);
  });
  if (!jobs.length) root.append(node("div", "empty-state compact", "还没有导出任务"));
}

async function loadSetup() {
  clearTimeout(state.setupTimer);
  try {
    const account = $("#setupAccount").value || state.setup?.selected?.wxid;
    const data = await api("/api/setup/status", {account});
    state.setup = data; renderSetup(data);
    if (["queued","running"].includes(data.job?.status)) {
      state.setupTimer = setTimeout(loadSetup, 900);
    }
  } catch (error) { toast(error.message); }
}

function renderSetup(data) {
  const select = $("#setupAccount"), selected = data.selected;
  const previous = selected?.wxid || select.value; select.replaceChildren();
  data.accounts.forEach((account) => {
    const option = node("option", "", `${account.wxid} · ${account.database_count} 个数据库`);
    option.value = account.wxid; select.append(option);
  });
  if (previous) select.value = previous;
  const keyReady = Boolean(selected?.key_count);
  const decryptedReady = Boolean(selected?.decrypted_count);
  const steps = [
    [Boolean(data.data_root), "发现微信数据目录", data.data_root || "未找到 xwechat_files"],
    [Boolean(selected), "选择账号", selected ? `${selected.database_count} 个加密数据库` : "没有可用账号"],
    [keyReady || data.memory_readable, "权限检查", keyReady ? "已有经过验证的密钥" : (data.memory_readable ? "当前用户可读取微信进程" : "取钥时将请求一次系统授权")],
    [keyReady, "提取并验证密钥", keyReady ? `${selected.key_count}/${selected.database_count} 个库已有密钥` : "尚未生成私有密钥文件"],
    [decryptedReady, "安全快照与解密", decryptedReady ? `${selected.decrypted_count} 个数据库可读` : "会自动复制 WAL 快照后再解密"],
    [data.account_archived, "建立索引与还原媒体", data.account_archived ? "账号已进入统一档案" : "等待前序步骤完成"],
  ];
  const root = $("#setupSteps"); root.replaceChildren();
  steps.forEach(([done,label,detail],index) => {
    const item = node("div", `setup-step${done ? " done" : ""}`);
    item.append(node("div", "step-mark", done ? "✓" : String(index+1)));
    const copy=node("div","step-copy");copy.append(node("b","",label),node("span","",detail));item.append(copy);root.append(item);
  });
  const command = selected?.key_command || "";
  $("#keyCommand").textContent = command;
  $("#keyCommand").classList.toggle("hidden", keyReady || !command);
  $("#copyKeyCommand").classList.toggle("hidden", keyReady || !command);
  const job = data.job || {status:"idle",message:""};
  const running = ["queued","running"].includes(job.status);
  const button = $("#setupRun"); button.disabled = running || !selected;
  button.textContent = data.account_archived ? "进入知识库" : (running ? "初始化进行中…" : (keyReady || decryptedReady ? "开始解密并建立索引" : "重新检测密钥"));
  const copy = $("#setupActionCopy"); copy.replaceChildren();
  copy.append(node("h3", "", data.account_archived ? "初始化已完成" : (running ? "正在建立档案" : (keyReady || decryptedReady ? "可以继续" : "先完成一次受限取钥"))),
    node("p", "", running || job.status === "failed" || job.status === "done" ? job.message :
      (keyReady || decryptedReady ? "下一步会完成快照、解密、媒体还原和全文索引。" : "复制下面的命令到终端运行。系统授权只用于读取微信进程内存。")));
  $("#setupBadge").lastChild.textContent = data.account_archived ? "已就绪" : (running ? "处理中" : "仅本机运行");
}

function switchView(view) {
  state.view=view;$$('.rail-btn[data-view]').forEach((item)=>item.classList.toggle("active",item.dataset.view===view));
  ["overview","analytics","directory","media","export","setup"].forEach((name) => {
    $(`#${name}View`).classList.toggle("hidden", view !== name);
  });
  const chat=view==="chat";
  $("#emptyState").classList.toggle("hidden",!chat||Boolean(state.session));
  $("#messageViewport").classList.toggle("hidden",!chat||!state.session);
  $("#filters").classList.toggle("hidden",!chat);
  document.querySelector(".conversation-head").classList.toggle("hidden",!chat);
  document.querySelector(".app-shell").classList.toggle("chat-open", !chat || Boolean(state.session));
  if(view==="directory") loadDirectory(true);
  if(view==="media") loadMedia(true);
  if(view==="analytics")loadAnalytics($("#analyticsYear").value);
  if(view==="export"){renderExportSessions();loadExportJobs();}else clearTimeout(state.exportTimer);
  if(view==="setup")loadSetup();else clearTimeout(state.setupTimer);
}

function openPreview(src){$("#mediaPreview").src=src;$("#mediaDialog").showModal();}

$("#accountSelect").addEventListener("change",async(event)=>{state.account=state.accounts.find((item)=>item.id===Number(event.target.value));state.session=null;state.messages=[];clearTimeFilter(false);$("#emptyState").classList.remove("hidden");$("#messageViewport").classList.add("hidden");await loadSessions();if(state.view==="directory")await loadDirectory(true);if(state.view==="media")await loadMedia(true);if(state.view==="analytics")await loadAnalytics();});
$("#searchInput").addEventListener("keydown",(event)=>{if(event.key==="Enter")runSearch(event.target.value.trim());});
let searchTimer;$("#searchInput").addEventListener("input",(event)=>{clearTimeout(searchTimer);if(!event.target.value.trim())searchTimer=setTimeout(()=>loadSessions(),180);});
$("#loadEarlier").addEventListener("click",()=>loadMessages(true));
$("#filters").addEventListener("click",(event)=>{const button=event.target.closest(".chip[data-kind]");if(!button)return;$$('#filters .chip[data-kind]').forEach((item)=>item.classList.remove("active"));button.classList.add("active");state.kind=button.dataset.kind;state.messages=[];state.oldest=0;loadMessages(false);});
$("#monthJump").addEventListener("change",(event)=>{if(!event.target.value){clearTimeFilter();return;}$("#dateJump").value="";const [year,month]=event.target.value.split("-").map(Number);const start=new Date(year,month-1,1),end=new Date(year,month,1);setTimeRange(start,end);});
$("#dateJump").addEventListener("change",(event)=>{if(!event.target.value){clearTimeFilter();return;}$("#monthJump").value="";const start=new Date(`${event.target.value}T00:00:00`),end=new Date(start);end.setDate(end.getDate()+1);setTimeRange(start,end);});
$("#clearDate").addEventListener("click",()=>clearTimeFilter());
$("#directoryTabs").addEventListener("click",(event)=>{const button=event.target.closest("[data-category]");if(!button)return;$$('#directoryTabs [data-category]').forEach((item)=>item.classList.toggle("active",item===button));state.directoryCategory=button.dataset.category;loadDirectory(true);});
let directoryTimer;$("#directorySearch").addEventListener("input",(event)=>{clearTimeout(directoryTimer);directoryTimer=setTimeout(()=>{state.directoryQuery=event.target.value.trim();loadDirectory(true);},180);});
$("#directoryMore").addEventListener("click",()=>loadDirectory(false));
$("#mediaTabs").addEventListener("click",(event)=>{const button=event.target.closest("[data-category]");if(!button)return;$$('#mediaTabs [data-category]').forEach((item)=>item.classList.toggle("active",item===button));state.mediaCategory=button.dataset.category;loadMedia(true);});
$("#mediaMore").addEventListener("click",()=>loadMedia(false));
$("#analyticsYear").addEventListener("change",(event)=>loadAnalytics(event.target.value));
$("#exportFormat").addEventListener("change",(event)=>{const global=["sqlite","contacts_csv","messages_csv"].includes(event.target.value);$("#exportSession").disabled=global;if(global)$("#exportSession").value="*";});
$("#exportForm").addEventListener("submit",async(event)=>{event.preventDefault();const session=$("#exportSession").value;if(!session){toast("请选择一个有消息的会话");return;}const button=$("#exportStart");button.disabled=true;try{await apiPost("/api/exports",{account:state.account.id,session,format:$("#exportFormat").value});toast("导出任务已开始");await loadExportJobs();}catch(error){toast(error.message);}finally{button.disabled=false;}});
$("#setupAccount").addEventListener("change",()=>loadSetup());
$("#copyKeyCommand").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("#keyCommand").textContent);toast("取钥命令已复制");}catch(_error){toast("无法自动复制，请手动选择命令文本");}});
$("#setupRun").addEventListener("click",async()=>{if(state.setup?.account_archived){if(state.accounts.length)switchView("overview");else location.reload();return;}await loadSetup();const selected=state.setup?.selected;if(!selected)return;if(!selected.key_count&&!selected.decrypted_count){toast("请先在终端运行受限取钥命令，完成后再点这里");return;}try{await apiPost("/api/setup/run",{account:selected.wxid});toast("初始化任务已开始");await loadSetup();}catch(error){toast(error.message);}});
$$('.rail-btn[data-view]').forEach((button)=>button.addEventListener("click",()=>switchView(button.dataset.view)));
$("#themeToggle").addEventListener("click",()=>{const dark=document.documentElement.dataset.theme!=="dark";document.documentElement.dataset.theme=dark?"dark":"light";localStorage.setItem("wxArchiveTheme",dark?"dark":"light");$("#themeToggle").textContent=dark?"夜":"昼";});
$("#detailToggle").addEventListener("click",()=>$("#detailPane").classList.toggle("open"));
$("#detailClose").addEventListener("click",()=>$("#detailPane").classList.remove("open"));
$("#mobileBack").addEventListener("click",()=>document.querySelector(".app-shell").classList.remove("chat-open"));
$("#mediaClose").addEventListener("click",()=>$("#mediaDialog").close());
$("#mediaDialog").addEventListener("click",(event)=>{if(event.target===$("#mediaDialog"))$("#mediaDialog").close();});

const savedTheme=localStorage.getItem("wxArchiveTheme");if(savedTheme)document.documentElement.dataset.theme=savedTheme;
$("#themeToggle").textContent=document.documentElement.dataset.theme==="dark"?"夜":"昼";
bootstrap();

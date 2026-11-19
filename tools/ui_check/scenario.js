// Checks run inside the page by run_ui_check.py. Each check drives the page's
// own functions (onConn, startTransfer, onWatch, refresh, loadLocal...) and
// reads its state; results go back to the runner through POST /report.
// /fs lets a check write a file inside the runner's temp folder, ask whether a
// file reached the server, delay the next call to a bridge method, or count
// bridge calls.
(async()=>{
const T=window.__T, checks=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function ok(name,fn,detail){ let pass=false,d=""; try{ pass=!!fn(); d=detail?String(detail()):""; }catch(e){ d="threw: "+e; } checks.push({name,pass,detail:d}); }
async function waitFor(fn,ms=10000){ const t=Date.now(); while(Date.now()-t<ms){ try{ if(await fn())return true; }catch(e){} await sleep(50);} return false; }
const post=(u,o)=>fetch(u,{method:"POST",body:JSON.stringify(o)}).then(r=>r.json());
const fs=(op,o)=>post("/fs",Object.assign({op},o||{}));
const names=side=>state[side].entries.map(e=>e.name);
const selNames=side=>[...state[side].sel].map(i=>state[side].entries[i]&&state[side].entries[i].name).sort();
function select(side,list){ const s=state[side]; s.sel=new Set(list.map(n=>s.entries.findIndex(e=>e.name===n))); s.anchor=[...s.sel][0]; renderPane(side); }
async function clickOk(){ await waitFor(()=>$("confirmModal").classList.contains("show"),3000); $("cfOk").click(); }
const batchDone=()=>waitFor(()=>$("qMeta").textContent.startsWith("Done:")&&queuePending===0,20000);
const consoleText=()=>$("console").textContent;
const consoleLines=()=>[...document.querySelectorAll("#console .cline")].map(el=>el.textContent);
try{
  await waitFor(()=>typeof API!=="undefined"&&API);
  const bottom=document.querySelector(".bottom-bar"), q=document.querySelector(".queue"), barIds=[...bottom.querySelectorAll("[id]")].map(el=>el.id).sort();
  ok("bottom bar has only the standard controls",()=>JSON.stringify(barIds)===JSON.stringify(["dbgToggle","updateBtn","updateNotice","verLabel"]),()=>barIds);
  ok("bottom bar does not overlap the queue",()=>bottom.getBoundingClientRect().top>=q.getBoundingClientRect().bottom,()=>JSON.stringify({queue:q.getBoundingClientRect().bottom,bar:bottom.getBoundingClientRect().top}));
  ok("debug and remember controls are checkboxes",()=>$("dbgToggle").type==="checkbox"&&$("rememberToggle").type==="checkbox");

  // The runner points the debug log at a folder that doesn't exist, so
  // turning it on always fails: a warning must reach the console, and the
  // switch must reflect that logging stayed off. Wait for init() to finish
  // wiring the checkbox before relying on its onchange handler.
  await waitFor(()=>typeof $("dbgToggle").onchange==="function");
  $("dbgToggle").click();
  const warned=await waitFor(()=>consoleText().includes("Debug log:"),5000);
  ok("a debug log write failure shows a warning in the console",()=>warned,()=>consoleText().slice(-300));
  ok("debug switch reflects logging staying off",()=>$("dbgToggle").checked===false);
  $("host").value="127.0.0.1"; $("user").value="test";
  await onConn();
  await waitFor(()=>state.remote.cwd==="/");
  ok("setup: connected, remote at /",()=>connected&&state.remote.cwd==="/");
  await loadLocal(T.local);
  ok("setup: local pane at the test folder",()=>state.local.cwd===T.local,()=>state.local.cwd);

  const originalPrompt2=prompt2;
  const localFile=()=>state.local.entries.find(e=>e.name==="a.txt");
  const remoteFile=()=>state.remote.entries.find(e=>e.name==="report.txt");
  const parentEntries=async()=>await API.list_local(T.local+"\\..");

  const c1Log=consoleText().length;
  prompt2=async()=>"..\\escaped.txt";
  await renameItem("local",localFile());
  prompt2=originalPrompt2;
  const c1Parent=await parentEntries();
  ok("C1: local rename refusal is shown and stays in the local folder",()=>consoleText().slice(c1Log).includes("Rename failed:")&&consoleText().slice(c1Log).includes("can't be used here")&&!c1Parent.entries.some(e=>e.name==="escaped.txt"),()=>consoleText().slice(c1Log));

  const c2Log=consoleText().length;
  prompt2=async()=>"..\\escaped";
  await newFolder("local");
  prompt2=originalPrompt2;
  const c2Parent=await parentEntries();
  ok("C2: local new-folder refusal is shown and creates nothing",()=>consoleText().slice(c2Log).includes("New folder failed:")&&consoleText().slice(c2Log).includes("can't be used here")&&!c2Parent.entries.some(e=>e.name==="escaped"),()=>consoleText().slice(c2Log));

  const c3Log=consoleText().length, c3Before=names("remote").slice().sort().join("|");
  prompt2=async()=>"../escaped.txt";
  await renameItem("remote",remoteFile());
  prompt2=originalPrompt2;
  const c3After=await API.list_remote(state.remote.cwd);
  ok("C3: remote rename refusal is shown and leaves the listing unchanged",()=>consoleText().slice(c3Log).includes("Rename failed:")&&consoleText().slice(c3Log).includes("can't be used here")&&c3Before===c3After.entries.map(e=>e.name).sort().join("|"),()=>consoleText().slice(c3Log));

  const hostileOpen={name:"..\\x",is_dir:false,size:0,mtime:0};
  state.local.entries.push(hostileOpen); renderPane("local");
  const c4Log=consoleText().length;
  const openRow=[...$("bodyLocal").querySelectorAll("tr")].find(row=>row.textContent.includes(hostileOpen.name));
  await openRow.ondblclick();
  const doubleClickLogged=await waitFor(()=>consoleText().slice(c4Log).includes("Open failed:")&&consoleText().slice(c4Log).includes("can't be used here"),3000);
  const c4ContextLog=consoleText().length;
  showCtx({clientX:0,clientY:0},"local",hostileOpen);
  [...$("ctx").querySelectorAll("button")].find(button=>button.textContent==="Open").click();
  const contextLogged=await waitFor(()=>consoleText().slice(c4ContextLog).includes("Open failed:")&&consoleText().slice(c4ContextLog).includes("can't be used here"),3000);
  ok("C4: hostile local Open reports refusal from double-click and context menu",()=>doubleClickLogged&&contextLogged,()=>consoleText().slice(c4Log));

  await fs("write",{path:T.local+"\\delete-real.txt",data:"delete"}); await loadLocal(T.local);
  const hostileDelete={name:"..",is_dir:true,size:0,mtime:0};
  state.local.entries.push(hostileDelete); renderPane("local"); select("local",[hostileDelete.name,"delete-real.txt"]);
  const c5Log=consoleLines().length, deleting=deleteSel("local"); await clickOk(); await deleting;
  const c5Deleted=await waitFor(async()=>!(await API.list_local(T.local)).entries.some(e=>e.name==="delete-real.txt"),3000);
  const c5Lines=consoleLines().slice(c5Log);
  ok("C5: mixed delete reports the hostile name separately and deletes the real file",()=>c5Deleted&&c5Lines.some(line=>line.includes("Deleted 1/2 from local, 1 could not be deleted"))&&c5Lines.some(line=>line.includes("..:")&&line.includes("can't be used here")),()=>c5Lines.join(" | "));

  select("remote",["report.txt"]);
  const namePrompt=startTransfer("download");
  const prompted=await waitFor(()=>$("diffModal").classList.contains("show"),3000);
  ok("case-variant download opens the overwrite prompt",()=>prompted);
  ok("case-variant download prompt shows local size",()=>$("dfBody").textContent.includes(hsize(state.local.entries.find(e=>e.name==="Report.txt").size)),()=>$("dfBody").textContent);
  $("dfCancel").click(); await namePrompt;

  await fs("queue_clash");
  $("qViewBtn").click();
  const clashRendered=await waitFor(()=>$("qList").textContent.includes("report.txt"),3000);
  ok("non-retryable clash row has no retry control",()=>clashRendered&&!$("qList").querySelector(".qretry"));
  ok("Retry all is disabled for only a clash row",()=>$("qRetryFailedBtn").disabled===true);
  qModalOpen=false; $("queueModal").classList.remove("show");

  await fs("queue_retry_refusal");
  $("qViewBtn").click();
  const refusalRendered=await waitFor(()=>$("qList").querySelector(".qretry"),3000);
  $("qList").querySelector(".qretry").click();
  const refusalShown=await waitFor(()=>consoleText().includes("That item can't be retried."),3000);
  ok("a refused retry shows a toast",()=>refusalRendered&&refusalShown,()=>consoleText());
  qModalOpen=false; $("queueModal").classList.remove("show");

  // The runner makes this one local subfolder unreadable in the real scanner,
  // matching the backend regression fixture without depending on Windows ACLs.
  await fs("unreadable_scan");
  await loadLocal(T.incomplete); select("local",["top"]);
  await startTransfer("upload");
  const incompleteLineFound=await waitFor(()=>[...document.querySelectorAll("#console .cline.scan-report")]
    .some(line=>line.textContent.includes("Scan incomplete")),20000);
  const incompleteLine=[...document.querySelectorAll("#console .cline.scan-report")]
    .find(line=>line.textContent.includes("Scan incomplete"));
  ok("incomplete scan console line is clickable",()=>incompleteLineFound&&!!incompleteLine&&incompleteLine.tabIndex===0&&incompleteLine.getAttribute("role")==="button",()=>consoleText());

  incompleteLine&&incompleteLine.click();
  const consoleListOpened=await waitFor(()=>$("scanProblemsModal").classList.contains("show")&&$("scanProblemsList").textContent.includes("denied"),5000);
  ok("clicking the incomplete scan console line opens its locations list",()=>consoleListOpened,()=>$("scanProblemsSummary").textContent+" | "+$("scanProblemsList").textContent);

  let copiedList="";
  try{ Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:async text=>{copiedList=text;}}}); }catch(e){}
  $("scanProblemsCopy").click();
  const copied=await waitFor(()=>consoleText().includes("List copied."),3000);
  ok("Copy list copies the summary and unreadable folder",()=>copied&&copiedList.includes($("scanProblemsSummary").textContent)&&copiedList.includes("denied"),()=>copiedList);

  $("scanProblemsClose").click();
  const closeWorks=!$("scanProblemsModal").classList.contains("show");
  $("qViewBtn").click();
  const reportRowFound=await waitFor(()=>[...$("qList").querySelectorAll(".qrow")].some(row=>row.textContent.includes("Scan incomplete")),5000);
  const reportRow=[...$("qList").querySelectorAll(".qrow")].find(row=>row.textContent.includes("Scan incomplete"));
  const reportButton=reportRow&&[...reportRow.querySelectorAll("button")].find(button=>button.textContent==="Show list");
  const noRetry=reportRow&&!reportRow.querySelector(".qretry");
  reportButton&&reportButton.click();
  const queueListOpened=await waitFor(()=>$("scanProblemsModal").classList.contains("show")&&$("scanProblemsList").textContent.includes("denied"),5000);
  ok("queue scan summary has Show list without Retry and opens the locations list",()=>reportRowFound&&!!reportButton&&noRetry&&queueListOpened,()=>reportRow&&reportRow.textContent);

  document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));
  ok("locations list closes with Close and Escape",()=>closeWorks&&!$("scanProblemsModal").classList.contains("show"));

  await openScanProblems(-1);
  ok("an unknown scan report shows its unavailable message",()=>$("scanProblemsModal").classList.contains("show")&&$("scanProblemsSummary").textContent==="This list is no longer available.",()=>$("scanProblemsSummary").textContent);
  $("scanProblemsClose").click(); qModalOpen=false; $("queueModal").classList.remove("show");
  await fs("readable_scan");
  await loadLocal(T.local);

  // Selection survives an automatic refresh that inserts rows above it.
  select("local",["b.txt","c.txt"]); select("remote",["0dl.txt","dl1.txt"]);
  $("qMeta").textContent="";
  await startTransfer("download");
  const dlDone=await batchDone(); ok("download batch finished",()=>dlDone,()=>$("qMeta").textContent);
  await waitFor(()=>names("local").includes("0dl.txt"),5000);
  ok("local pane shows downloaded files without Refresh",()=>names("local").includes("0dl.txt")&&names("local").includes("dl1.txt"),()=>names("local"));
  ok("local selection still b.txt and c.txt after refresh",()=>JSON.stringify(selNames("local"))==='["b.txt","c.txt"]',()=>selNames("local"));
  ok("remote selection kept after refresh",()=>JSON.stringify(selNames("remote"))==='["0dl.txt","dl1.txt"]',()=>selNames("remote"));

  // A slow refresh reply must not overwrite a newer navigation.
  await fs("delay",{method:"list_local",ms:1500});
  refresh("local"); await sleep(150);
  await loadLocal(T.sub); await sleep(2200);
  ok("local: late refresh reply dropped, pane stays in sub",()=>state.local.cwd===T.sub&&names("local").includes("insub.txt")&&!names("local").includes("a.txt"),()=>state.local.cwd+" "+names("local"));
  ok("local: path shown matches the files shown",()=>$("crumbsLocal").textContent===T.sub,()=>$("crumbsLocal").textContent);
  await loadLocal(T.local);
  await fs("delay",{method:"list_remote",ms:1500});
  refresh("remote"); await sleep(150);
  await loadRemote("/other"); await sleep(2200);
  ok("remote: late refresh reply dropped, pane stays in /other",()=>state.remote.cwd==="/other"&&names("remote").includes("o.txt"),()=>state.remote.cwd+" "+names("remote"));
  await loadRemote("/");

  // Compare summaries stay when browsing within roots, then clear as soon as
  // either a refresh or navigation leaves the roots.
  await onCompare();
  const compared=await waitFor(()=>$("localCompare").textContent.includes("newer here")&&$("remoteCompare").textContent.includes("newer remote"),20000);
  ok("compare summaries appear in both pane titles",()=>compared,()=>$("localCompare").textContent+" | "+$("remoteCompare").textContent);
  await waitFor(()=>!qWasBusy,10000); await sleep(1500);
  ok("compare colors and summaries survive the compare finishing",()=>!!compareMap&&$("localCompare").textContent!==""&&$("remoteCompare").textContent!=="");
  // The download batch above left running totals in the queue; a Compare
  // moves no files, so it must not repeat that batch's Done line.
  ok("a bare Compare after a batch leaves the Done line blank",()=>$("qMeta").textContent==="",()=>$("qMeta").textContent);

  // Narrowest window the app allows (min_size 1000 wide), with the compare
  // counts and a long watch tag showing: bar stays one row, titles one line.
  const app=document.querySelector(".app"), bar=document.querySelector(".bottom-bar");
  app.style.width="1000px"; $("watchTag").textContent="Watching → /srv/releases/storefront/current/a/very/long/folder/name";
  await sleep(100);
  const br=bar.getBoundingClientRect(), inBar=el=>{const r=el.getBoundingClientRect();return r.left>=br.left-0.5&&r.right<=br.right+0.5&&r.top>=br.top-0.5&&r.bottom<=br.bottom+0.5;};
  ok("narrow: bottom bar stays one 44px row",()=>Math.round(br.height)===44,()=>br.height);
  ok("narrow: bottom bar contents stay inside it",()=>[...bar.querySelectorAll(".bar-left,.bar-right,#updateBtn,#verLabel,.dbg-toggle")].every(inBar));
  const oneLine=el=>el.getBoundingClientRect().height<24;
  ok("narrow: pane titles stay one line",()=>[...document.querySelectorAll(".pane-title")].every(oneLine),()=>[...document.querySelectorAll(".pane-title")].map(e=>e.getBoundingClientRect().height));
  ok("narrow: long watch tag is cut off, not overflowing",()=>{const t=$("watchTag"),p=t.parentElement.getBoundingClientRect(),r=t.getBoundingClientRect();return r.right<=p.right+0.5&&t.scrollWidth>t.clientWidth;});
  app.style.width=""; $("watchTag").textContent="";

  // The update notice sits on the bar's true center, not shifted by the
  // wider right-hand zone.
  $("updateNotice").textContent="You're on the latest version"; await sleep(50);
  const nr=$("updateNotice").getBoundingClientRect(), fr=bar.getBoundingClientRect();
  ok("update notice is centered in the bottom bar",()=>Math.abs((nr.left+nr.right)/2-(fr.left+fr.right)/2)<=2,()=>((nr.left+nr.right)/2-(fr.left+fr.right)/2).toFixed(1)+"px off");
  $("updateNotice").textContent="";

  // Keyboard focus shows the teal outline.
  const outlined=el=>getComputedStyle(el).outlineStyle==="solid";
  $("dbgToggle").focus(); ok("focus: debug switch shows an outline",()=>outlined(document.querySelector(".dbg-track")));
  $("rememberToggle").focus(); ok("focus: remember switch shows an outline",()=>outlined(document.querySelector(".remember-track")));
  $("updateBtn").focus(); ok("focus: update button shows an outline",()=>outlined($("updateBtn")));
  $("theme-btn").focus(); ok("focus: theme button shows an outline",()=>outlined($("theme-btn")));
  $("compareBtn").focus(); ok("focus: pane buttons show an outline",()=>outlined($("compareBtn")));
  document.activeElement.blur();
  refresh("remote"); await waitFor(()=>!state.remote.refreshing&&!compareMap,5000);
  ok("one manual refresh right after a compare clears it",()=>!compareMap&&$("localCompare").textContent==="",()=>$("localCompare").textContent);
  await onCompare(); await waitFor(()=>!!compareMap,20000); await waitFor(()=>!qWasBusy,10000);
  await loadLocal(T.sub);
  ok("compare summaries remain in compared subfolder",()=>!!compareMap&&$("localCompare").textContent!=="");
  await loadLocal(T.other);
  ok("compare summaries clear outside compared root",()=>!compareMap&&$("localCompare").textContent===""&&$("remoteCompare").textContent==="");
  await loadLocal(T.local);
  applyCompareResult({root_local:T.local,root_remote:"/",files:{"a.txt":"newer_local"},folders:{}});
  refresh("local"); await waitFor(()=>!state.local.refreshing&&!compareMap);
  ok("compare summaries clear after pane refresh",()=>!compareMap&&$("localCompare").textContent===""&&$("remoteCompare").textContent==="");

  // A compare that finishes with an error must clear any earlier compare's
  // colors and counts, not leave a stale result showing under a fresh toast.
  applyCompareResult({root_local:T.local,root_remote:"/",files:{"a.txt":"newer_local"},folders:{}});
  ok("setup: seeded a compare result before the failing compare",()=>!!compareMap&&$("localCompare").textContent!=="");
  startQueuePoll();
  const missingLocal=T.local+"\\does_not_exist";
  const cr=await API.compare(missingLocal,state.remote.cwd);
  ok("failing compare starts normally",()=>cr.ok,()=>JSON.stringify(cr));
  const failedCleared=await waitFor(()=>!compareMap&&$("localCompare").textContent===""&&$("remoteCompare").textContent==="",20000);
  ok("a failed compare clears the previous compare's colors and counts",()=>failedCleared,()=>$("localCompare").textContent+" | "+$("remoteCompare").textContent);

  // Starting Watch must not rerun the end-of-batch refresh.
  const before=await fs("calls");
  const w=onWatch(); await clickOk(); await w; await sleep(2000);
  const after=await fs("calls");
  ok("watch started",()=>watching===true);
  ok("watch tag shows while watching",()=>$("watchTag").textContent.includes("Watching →")&&$("watchTag").title.includes(state.local.cwd)&&$("watchTag").title.includes(state.remote.cwd),()=>$("watchTag").textContent);
  ok("starting Watch triggers no pane refresh",()=>(after.list_local||0)===(before.list_local||0)&&(after.list_remote||0)===(before.list_remote||0),()=>JSON.stringify({before,after}));

  // Watcher upload into the folder the remote pane shows.
  await fs("write",{path:T.local+"\\w1.txt",data:"w1"});
  const t0=Date.now();
  const seen=await waitFor(()=>names("remote").includes("w1.txt"),10000);
  ok("watcher upload appears in remote pane without Refresh",()=>seen,()=>"after "+(Date.now()-t0)+" ms");
  ok("watcher console line shown",()=>consoleText().includes("Watch: uploaded w1.txt"));

  // Watcher upload while the remote pane shows another folder.
  await loadRemote("/other");
  await fs("write",{path:T.local+"\\w2.txt",data:"w2"});
  await waitFor(async()=>(await fs("exists",{rel:"w2.txt"})).ok,10000); await sleep(1500);
  ok("remote pane on another folder stays put",()=>state.remote.cwd==="/other"&&$("crumbsRemote").textContent==="/other"&&!names("remote").includes("w2.txt"),()=>state.remote.cwd+" "+names("remote"));
  await loadRemote("/");

  // Queue upload while watching: the footer's Done line stays.
  await loadLocal(T.other); select("local",["up.txt"]);
  $("qMeta").textContent="";
  await startTransfer("upload"); await batchDone();
  const meta=$("qMeta").textContent; await sleep(3000);
  ok("footer Done line stays while watching",()=>/^Done: \d+ completed/.test(meta)&&$("qMeta").textContent===meta,()=>meta+" | "+$("qMeta").textContent);
  ok("uploaded file shows in remote pane",()=>names("remote").includes("up.txt"),()=>names("remote"));
  ok("poll slowed to once a second while only watching",()=>qPollDelay===1000&&!!qPollTimer);

  await onWatch();
  ok("watch tag hides after stop",()=>!watching&&$("watchTag").textContent==="");
  const restart=onWatch(); await clickOk(); await restart;

  // Disconnect while watching.
  await onConn(); await sleep(1500);
  ok("Watch button reset after disconnect",()=>!watching&&$("watchBtn").textContent==="Watch",()=>$("watchBtn").textContent);
  ok("watch tag hides after disconnect",()=>$("watchTag").textContent==="");
  ok("poll stopped after disconnect",()=>!qPollTimer);
  await post("/report",{checks});
}catch(e){ await post("/report",{checks,error:String(e&&e.stack||e)}); }
})();

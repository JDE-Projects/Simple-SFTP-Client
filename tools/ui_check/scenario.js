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
try{
  await waitFor(()=>typeof API!=="undefined"&&API);
  $("host").value="127.0.0.1"; $("user").value="test";
  await onConn();
  await waitFor(()=>state.remote.cwd==="/");
  ok("setup: connected, remote at /",()=>connected&&state.remote.cwd==="/");
  await loadLocal(T.local);
  ok("setup: local pane at the test folder",()=>state.local.cwd===T.local,()=>state.local.cwd);

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

  // Starting Watch must not rerun the end-of-batch refresh.
  const before=await fs("calls");
  const w=onWatch(); await clickOk(); await w; await sleep(2000);
  const after=await fs("calls");
  ok("watch started",()=>watching===true);
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

  // Disconnect while watching.
  await onConn(); await sleep(1500);
  ok("Watch button reset after disconnect",()=>!watching&&$("watchBtn").textContent==="Watch",()=>$("watchBtn").textContent);
  ok("poll stopped after disconnect",()=>!qPollTimer);
  await post("/report",{checks});
}catch(e){ await post("/report",{checks,error:String(e&&e.stack||e)}); }
})();

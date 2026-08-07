var currentSearchType = 'songs';
var activeTab = 'search';

function getApiUrl(path){
  var base = window.location.pathname;
  if (!base.endsWith("/")) base += "/";
  return base + path.replace(/^\//, "");
}

async function checkAuth(){
  try{
    var r = await fetch(getApiUrl("api/auth_status"));
    var d = await r.json();
    if(d.authenticated){
      document.getElementById("login-modal").style.display = "none";
      updateBadge();
    }else{
      document.getElementById("login-modal").style.display = "flex";
    }
  }catch(e){
    document.getElementById("login-modal").style.display = "flex";
  }
}

async function doLogin(e){
  e.preventDefault();
  var pwd = document.getElementById("login-password").value;
  var errEl = document.getElementById("login-error");
  errEl.style.display = "none";

  try{
    var r = await fetch(getApiUrl("api/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pwd })
    });
    var d = await r.json();
    if(r.status === 200 && d.status === "ok"){
      document.getElementById("login-modal").style.display = "none";
      toast("Accesso effettuato");
      updateBadge();
    }else{
      errEl.style.display = "block";
    }
  }catch(err){
    errEl.style.display = "block";
  }
}

function setSearchType(type){
  currentSearchType = type;
  document.getElementById("btn-type-songs").classList.toggle("active", type==='songs');
  document.getElementById("btn-type-albums").classList.toggle("active", type==='albums');
}

function switchTab(el,id){
  activeTab = id;
  document.querySelectorAll(".tab").forEach(function(t){t.classList.remove("active")});
  document.querySelectorAll(".nav-btn").forEach(function(b){b.classList.remove("active")});
  document.getElementById("tab-"+id).classList.add("active");
  el.classList.add("active");
  if(id==="downloads")loadDL();
  if(id==="library")loadLib();
  if(id==="storico")loadHistory();
}

async function doSearch(e){
  e.preventDefault();
  var q=document.getElementById("q").value;
  var c=document.getElementById("results");
  c.innerHTML='<p style="text-align:center;width:100%;padding:40px;font-size:1.2rem;color:var(--m3-on-surface-variant)"><span class="material-symbols-rounded" style="animation:spin 1s linear infinite">sync</span> Ricerca '+currentSearchType+' in corso...</p>';
  try{
    var r=await fetch(getApiUrl("api/search?q="+encodeURIComponent(q)+"&type="+currentSearchType));
    if(r.status === 401){
      document.getElementById("login-modal").style.display = "flex";
      return;
    }
    var d=await r.json();
    if(d.status==="ok" && d.results.length>0){
      if(d.searchType === "albums"){
        c.innerHTML=d.results.map(function(alb){
          var t=alb.title.replace(/"/g,'&quot;');
          var a=alb.artist.replace(/"/g,'&quot;');
          return '<div class="card"><div class="card-img-wrap"><img src="'+alb.thumbnail+'" class="card-img" loading="lazy"></div><div class="card-body"><div class="card-title"><span class="material-symbols-rounded">album</span> '+alb.title+'</div><div class="card-artist">'+alb.artist+'</div><div class="card-album">Anno: '+(alb.year||'N/D')+'</div><button class="btn-dl" data-bid="'+alb.browseId+'" data-title="'+t+'" data-artist="'+a+'" onclick="dlAlbum(this)"><span class="material-symbols-rounded">download_for_offline</span> Scarica Album Intero</button></div></div>';
        }).join("");
      }else{
        c.innerHTML=d.results.map(function(s){
          var t=s.title.replace(/"/g,'&quot;');
          var a=s.artist.replace(/"/g,'&quot;');
          var al=s.album?s.album.replace(/"/g,'&quot;'):'Singolo';
          var btnClass = s.inLibrary ? "btn-dl done" : "btn-dl";
          var btnText = s.inLibrary ? '<span class="material-symbols-rounded">&#xe86c;</span> In Libreria' : '<span class="material-symbols-rounded">&#xf090;</span> Scarica MP3';
          var btnClick = s.inLibrary ? "" : 'onclick="dl(this)"';
          return '<div class="card"><div class="card-img-wrap"><img src="'+s.thumbnail+'" class="card-img" loading="lazy"></div><div class="card-body"><div class="card-title">'+s.title+'</div><div class="card-artist">'+s.artist+'</div><div class="card-album">'+al+' &bull; '+s.duration+'</div><button class="'+btnClass+'" data-vid="'+s.videoId+'" data-title="'+t+'" data-artist="'+a+'" data-album="'+al+'" '+btnClick+'>'+btnText+'</button></div></div>';
        }).join("");
      }
    }else{
      c.innerHTML='<p style="text-align:center;width:100%;padding:40px">Nessun risultato trovato</p>';
    }
  }catch(err){
    c.innerHTML='<p style="color:var(--m3-error);text-align:center;width:100%;padding:40px">Errore durante la ricerca</p>';
  }
}

async function dl(btn){
  var vid=btn.getAttribute("data-vid");
  var title=btn.getAttribute("data-title");
  var artist=btn.getAttribute("data-artist");
  var album=btn.getAttribute("data-album");
  btn.classList.add("done");
  btn.innerHTML='<span class="material-symbols-rounded">&#xe86c;</span> Avviato';
  toast("Download: "+artist+" - "+title);
  try{
    await fetch(getApiUrl("api/download"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({videoId:vid,title:title,artist:artist,album:album})});
    updateBadge();
  }catch(err){toast("Errore download")}
}

async function dlAlbum(btn){
  var bid=btn.getAttribute("data-bid");
  var title=btn.getAttribute("data-title");
  var artist=btn.getAttribute("data-artist");
  btn.classList.add("done");
  btn.innerHTML='<span class="material-symbols-rounded">&#xe86c;</span> Album Avviato';
  toast("Download Album: "+artist+" - "+title);
  try{
    var r = await fetch(getApiUrl("api/download_album"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({browseId:bid,title:title,artist:artist})});
    var d = await r.json();
    if(d.status==="ok"){
      toast("In coda "+d.queued+" brani dell'album "+d.album);
    }
    updateBadge();
  }catch(err){toast("Errore download album")}
}

async function cancelDL(taskId){
  try{
    await fetch(getApiUrl("api/download/cancel"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({taskId:taskId})});
    toast("Download annullato");
    loadDL();
  }catch(e){}
}

async function loadDL(){
  var c=document.getElementById("dl-list");
  try{
    var r=await fetch(getApiUrl("api/downloads"));
    if(r.status===401) return;
    var d=await r.json();
    if(d.downloads.length===0){c.innerHTML='<p style="padding:20px;color:var(--m3-on-surface-variant)">Nessun download in coda</p>';return}
    c.innerHTML=d.downloads.slice().reverse().map(function(x){
      var statusColor = x.status==="completed"?"#ffffff":x.status==="exists"?"#a1a1aa":x.status==="retrying"?"#f59e0b":x.status==="failed"?"#ef4444":"var(--m3-on-surface-variant)";
      var statusText = x.status==="exists"?"Gia in libreria":x.status==="retrying"?"Riprovo...":x.status;
      var extra = x.message ? " - "+x.message : "";
      return '<div class="dl-item"><div style="flex:1;margin-right:15px;"><b>'+x.artist+' - '+x.title+'</b><br><span style="color:var(--m3-on-surface-variant)">Album: '+x.album+' &bull; </span><span style="color:'+statusColor+'">'+statusText+extra+'</span><div class="progress-bg"><div class="progress-fill" style="width:'+x.progress+'%"></div></div></div><div style="display:flex;align-items:center;gap:12px;"><b style="color:#ffffff">'+x.progress+'%</b><button class="btn-cancel" onclick="cancelDL(\''+x.id+'\')" title="Annulla"><span class="material-symbols-rounded">&#xeb99;</span></button></div></div>';
    }).join("");
  }catch(e){}
}

async function loadLib(){
  var c=document.getElementById("lib-list");
  try{
    var r=await fetch(getApiUrl("api/library"));
    if(r.status===401) return;
    var d=await r.json();
    if(d.library.length===0){c.innerHTML='<p style="padding:20px;color:var(--m3-on-surface-variant)">Libreria vuota</p>';return}
    c.innerHTML=d.library.map(function(x){
      return '<div class="lib-card"><div class="lib-artist"><span class="material-symbols-rounded" style="color:#ffffff">&#xe7fd;</span> '+x.artist+'</div><div class="lib-count">'+x.totalTracks+' brani ('+x.albums.length+' album)</div></div>';
    }).join("");
  }catch(e){}
}

async function loadHistory(){
  var c=document.getElementById("hist-list");
  try{
    var r=await fetch(getApiUrl("api/history"));
    if(r.status===401) return;
    var d=await r.json();
    if(!d.history || d.history.length===0){c.innerHTML='<p style="padding:20px;color:var(--m3-on-surface-variant)">Nessun download registrato ancora</p>';return}
    c.innerHTML=d.history.map(function(x){
      var auto = x.source === "auto";
      var badge = auto ? '<span class="badge badge-auto">Auto added</span>' : '<span class="badge badge-manual">Downloaded</span>';
      var statusColor = x.status==="completed"?"#4ade80":x.status==="exists"?"#a1a1aa":x.status==="failed"?"#ef4444":"#f59e0b";
      return '<div class="dl-item"><div style="flex:1;margin-right:15px;"><b>'+x.artist+' - '+x.title+'</b><br><span style="color:var(--m3-on-surface-variant)">'+x.ts+' &bull; Album: '+(x.album||'Singolo')+' &bull; </span><span style="color:'+statusColor+'">'+x.status+'</span></div>'+badge+'</div>';
    }).join("");
  }catch(e){}
}

async function updateBadge(){
  try{
    var r=await fetch(getApiUrl("api/downloads"));
    if(r.status===401) return;
    var d=await r.json();
    document.getElementById("dl-count").innerText=d.downloads.length;
    if(activeTab === 'downloads'){
      loadDL();
    }
  }catch(e){}
}

function toast(m){
  var t=document.createElement("div");t.className="toast";t.innerText=m;
  document.getElementById("toasts").appendChild(t);
  setTimeout(function(){t.remove()},3500);
}

checkAuth();
setInterval(updateBadge, 2000);

async function doLogout(){
  try{
    await fetch(getApiUrl("api/logout"), { method: "POST" });
    toast("Disconnesso");
    document.getElementById("login-modal").style.display = "flex";
  }catch(e){
    document.getElementById("login-modal").style.display = "flex";
  }
}

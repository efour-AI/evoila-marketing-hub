/* evoila hub: one tracker for every page.
   Logs to the Hub Activity table through the Author Desk API's "track" route.
   - Page view on load (which page, which post/deck it was opened for)
   - Every action the page sends (approve, hold, request a deck, send the one-pager ...)
   - File downloads and outside links that the page does not already track
   - Time spent on the page when the person leaves (only if 10 seconds or more)
   Pages can add their own: hubTrack(type, item), hubTrack.once(type, item),
   and name records with window.hubLabel = id => "Post title". */
(function(){
  var API="https://hook.us2.make.com/o28rqytj8ifgm1o2tckavp2194d8jbqt";
  var DEV=/Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)?"phone":"desktop";
  var origFetch=window.fetch.bind(window);
  function ls(k){try{return localStorage.getItem(k)}catch(e){return null}}
  /* read the personal link before the page strips it from the address bar */
  var hp=new URLSearchParams(location.hash.slice(1)),hashTok=hp.get("k")||hp.get("t");
  function token(){
    if(hashTok)return hashTok;
    var s=ls("evoila_hub");if(s)return s.slice(2);
    return ls("evoila_desk_t")||ls("evoila_desk_k")||null;
  }
  /* page name from the path: /evoila-marketing-hub/review/ -> review */
  var seg=location.pathname.replace(/index\.html$/,"").split("/").filter(Boolean).pop()||"";
  var PAGES={"evoila-marketing-hub":"Hub","hub":"Hub","review":"Post review","deckreview":"Deck review","deck":"Walking deck form",
    "build":"Build your own deck","convert":"Convert your deck","onepager":"One-pager","create":"Self-serve post",
    "authors":"Author desk","voice":"Voice","answer":"Ask the hub answer","research":"Explore research"};
  var PAGE=PAGES[seg]||seg||"Hub";

  function send(type,item){
    var t=token();if(!t)return;
    try{origFetch(API,{method:"POST",keepalive:true,body:new URLSearchParams({t:t,dev:DEV,a:"track",type:type,item:String(item||"").slice(0,190)})});}catch(e){}
  }
  var seen={};
  function hubTrack(type,item){send(type,item);}
  hubTrack.once=function(type,item){var k=type+"|"+item;if(seen[k])return;seen[k]=1;send(type,item);};
  hubTrack.page=PAGE;
  window.hubTrack=hubTrack;

  /* ---- page view ---- */
  var qp=new URLSearchParams(location.search),ctx=qp.get("p")||qp.get("id")||qp.get("d")||qp.get("deck")||hp.get("p")||hp.get("id")||"";
  if(PAGE!=="Hub")send("Page view",PAGE+(ctx?" · "+ctx:""));   /* the hub home logs its own Visit */

  /* ---- actions: watch what the page sends ---- */
  function label(id){try{return (id&&window.hubLabel&&window.hubLabel(id))||""}catch(e){return""}}
  function rec(p){var id=p.get("id")||"",n=label(id);return n?n:(p.get("table")?p.get("table")+" ":"")+id;}
  var ACT={
    approve:function(p){return["Post review","Approved: "+rec(p)]},
    changes:function(p){return["Post review","Sent back to marketing: "+rec(p)+(p.get("notes")?" · "+p.get("notes"):"")]},
    save:function(p){return["Post review","Saved edits: "+rec(p)]},
    hold:function(p){return["Post review","Held until "+(p.get("hold")||"?")+": "+rec(p)]},
    postnow:function(p){return["Post review","Posted now: "+rec(p)]},
    decline:function(p){return["Post review","Declined: "+rec(p)]},
    airewrite:function(p){return["Post review","AI rewrite: "+(p.get("instr")||"")]},
    "new":function(p){return["Self-serve post","Submitted: "+(p.get("title")||"")]},
    selfpost:function(p){return["Self-serve post","Submitted: "+(p.get("title")||String(p.get("bullets")||"").slice(0,80))]},
    custom:function(p){return["Custom deck","Submitted: "+(p.get("title")||"")+(p.get("company")?" for "+p.get("company"):"")]},
    convert:function(p){return["Convert deck","Uploaded a deck"+(p.get("company")?" for "+p.get("company"):"")]},
    ask:function(p){return["Ask marketing","Sent: "+(p.get("title")||"")]},
    taskupdate:function(p){return["Tasks",(p.get("status")==="Closed"?"Closed task":"Updated task ("+(p.get("status")||"")+")")+(p.get("update")?": "+p.get("update"):"")]},
    closetask:function(p){return["Tasks","Closed task"]},
    linktask:function(p){return["Tasks","Linked task to the plan"]},
    opedit:function(p){return["One-pager","Asked for a new version: "+(p.get("req")||"")]},
    oprestore:function(p){return["One-pager","Restored an older version"]},
    opsend:function(p){return["One-pager","Sent to "+(p.get("name")||p.get("to")||"someone")]},
    opcopy:function(p){return["One-pager","Downloaded a copy"]},
    opcontact:function(p){return["One-pager","Edited contacts"]},
    mstatus:function(p){return["Admin","Set "+rec(p)+" to "+(p.get("status")||"")]},
    msave:function(p){return["Admin","Saved "+rec(p)]},
    mreq:function(p){return["Admin","Replied to a request"]},
    plan:function(p){return["Admin","Updated the plan: "+(p.get("status")||"")]},
    addres:function(p){return["Admin","Added resource: "+(p.get("title")||"")]}
  };
  /* Airtable webhooks the pages call directly */
  var HOOKS=[
    [/wflWkbtgfhGOWOhfA/,function(p){return["Request deck","Walking deck for "+(p.get("company")||"?")+(p.get("audience")?" ("+p.get("audience")+")":"")]}],
    [/wflu5dMDA383qAPf9/,function(p){return["Post review","Redrew image: "+rec(p)]}]
  ];
  function params(body){
    try{
      if(!body)return null;
      if(body instanceof URLSearchParams)return body;
      if(typeof FormData!=="undefined"&&body instanceof FormData){var u=new URLSearchParams();body.forEach(function(v,k){if(typeof v==="string")u.append(k,v);});return u;}
      if(typeof body==="string")return new URLSearchParams(body);
    }catch(e){}
    return null;
  }
  window.fetch=function(url,opts){
    var res=origFetch(url,opts);
    try{
      var u=String(url&&url.url||url),p=params(opts&&opts.body);
      if(p&&p.get("a")!=="track"){
        var f=null;
        if(/hook\.us2\.make\.com/.test(u)&&ACT[p.get("a")])f=ACT[p.get("a")];
        else HOOKS.forEach(function(h){if(h[0].test(u))f=h[1];});
        if(f){var out=f(p);res.then(function(r){if(!r||r.type==="opaque"||r.ok)send(out[0],out[1]);else send(out[0],"FAILED "+out[1]);},function(){send(out[0],"FAILED "+out[1]);});}
      }
    }catch(e){}
    return res;
  };

  /* ---- downloads and outside links the page doesn't already track ---- */
  document.addEventListener("click",function(e){
    var a=e.target&&e.target.closest&&e.target.closest("a[href]");if(!a||a.onclick||a.dataset.notrack!=null)return;
    var h=a.getAttribute("href")||"",name=(a.getAttribute("download")||a.textContent||"").trim().slice(0,80);
    if(a.hasAttribute("download")||/\.(pptx|pdf|docx|xlsx|png|jpg|zip)(\?|$)/i.test(h)||/airtableusercontent|dl\.airtable/.test(h))send("Download",PAGE+": "+(name||h.split("/").pop()));
    else if(/^https?:/.test(h)&&a.host!==location.host)send("Link opened",PAGE+": "+(name?name+" → ":"")+a.host);
  },true);

  /* ---- time on page ---- */
  var start=Date.now(),active=0,last=Date.now(),vis=!document.hidden,sent=false;
  document.addEventListener("visibilitychange",function(){
    if(document.hidden){if(vis)active+=Date.now()-last;vis=false;leave();}
    else{vis=true;last=Date.now();sent=false;}
  });
  function leave(){
    if(sent)return;var s=Math.round(active/1000);if(s<10)return;sent=true;
    send("Time on page",PAGE+" · "+s+"s");active=0;
  }
  window.addEventListener("pagehide",function(){if(vis){active+=Date.now()-last;vis=false;}leave();});
})();

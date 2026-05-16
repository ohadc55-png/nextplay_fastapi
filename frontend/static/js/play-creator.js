/**
 * NextPlay - Play Creator (Vanilla JS)
 * Full play diagramming with animation, bezier curves, templates.
 * Adapted from HOOPS AI — standalone, no i18n, no external deps.
 */

/* ── Toast Utility ────────────────────────────────────────── */
const Toast={
  _show(msg,type){const el=document.createElement('div');el.className='pc-toast pc-toast-'+type;el.textContent=msg;document.body.appendChild(el);setTimeout(()=>el.classList.add('show'),10);setTimeout(()=>{el.classList.remove('show');setTimeout(()=>el.remove(),300);},3000);},
  success(msg){this._show(msg,'success');},
  error(msg){this._show(msg,'error');},
};

/* ── Formation Templates ──────────────────────────────────── */
const OFFENSE = {
  'empty':{name:'Empty',players:[{id:'o1',number:'1',x:50,y:78,type:'offense'},{id:'o2',number:'2',x:30,y:78,type:'offense'},{id:'o3',number:'3',x:70,y:78,type:'offense'},{id:'o4',number:'4',x:40,y:70,type:'offense'},{id:'o5',number:'5',x:60,y:70,type:'offense'}]},
  '5-out':{name:'5-Out',players:[{id:'o1',number:'1',x:50,y:75,type:'offense'},{id:'o2',number:'2',x:15,y:52,type:'offense'},{id:'o3',number:'3',x:85,y:52,type:'offense'},{id:'o4',number:'4',x:8,y:15,type:'offense'},{id:'o5',number:'5',x:92,y:15,type:'offense'}]},
  '4-out-1-in':{name:'4-Out 1-In',players:[{id:'o1',number:'1',x:62,y:58,type:'offense'},{id:'o2',number:'2',x:8,y:15,type:'offense'},{id:'o3',number:'3',x:92,y:15,type:'offense'},{id:'o4',number:'4',x:25,y:55,type:'offense'},{id:'o5',number:'5',x:50,y:18,type:'offense'}]},
  'horns':{name:'Horns',players:[{id:'o1',number:'1',x:50,y:75,type:'offense'},{id:'o2',number:'2',x:8,y:15,type:'offense'},{id:'o3',number:'3',x:92,y:15,type:'offense'},{id:'o4',number:'4',x:35,y:45,type:'offense'},{id:'o5',number:'5',x:65,y:45,type:'offense'}]},
  'box':{name:'Box',players:[{id:'o1',number:'1',x:50,y:82,type:'offense'},{id:'o2',number:'2',x:35,y:38,type:'offense'},{id:'o3',number:'3',x:65,y:38,type:'offense'},{id:'o4',number:'4',x:35,y:18,type:'offense'},{id:'o5',number:'5',x:65,y:18,type:'offense'}]},
  '1-4-high':{name:'1-4 High',players:[{id:'o1',number:'1',x:50,y:75,type:'offense'},{id:'o2',number:'2',x:8,y:45,type:'offense'},{id:'o3',number:'3',x:92,y:45,type:'offense'},{id:'o4',number:'4',x:35,y:45,type:'offense'},{id:'o5',number:'5',x:65,y:45,type:'offense'}]},
};
const DEFENSE = {
  'none':{name:'No Defense',players:[]},
  'man':{name:'Man-to-Man',players:[{id:'d1',number:'X1',x:50,y:72,type:'defense'},{id:'d2',number:'X2',x:18,y:52,type:'defense'},{id:'d3',number:'X3',x:82,y:52,type:'defense'},{id:'d4',number:'X4',x:30,y:28,type:'defense'},{id:'d5',number:'X5',x:70,y:28,type:'defense'}]},
  '23':{name:'2-3 Zone',players:[{id:'d1',number:'X1',x:35,y:62,type:'defense'},{id:'d2',number:'X2',x:65,y:62,type:'defense'},{id:'d3',number:'X3',x:20,y:32,type:'defense'},{id:'d4',number:'X4',x:50,y:25,type:'defense'},{id:'d5',number:'X5',x:80,y:32,type:'defense'}]},
  '32':{name:'3-2 Zone',players:[{id:'d1',number:'X1',x:50,y:65,type:'defense'},{id:'d2',number:'X2',x:25,y:55,type:'defense'},{id:'d3',number:'X3',x:75,y:55,type:'defense'},{id:'d4',number:'X4',x:35,y:25,type:'defense'},{id:'d5',number:'X5',x:65,y:25,type:'defense'}]},
};
const ACTIONS = {
  pass:{name:'Pass',icon:'\u2933',color:'#FBBF24',move:false,ball:true},
  dribble:{name:'Dribble',icon:'\u3030',color:'#34D399',move:true,ball:true},
  cut:{name:'Cut',icon:'\u2192',color:'#F472B6',move:true,ball:false},
  screen:{name:'Screen',icon:'\u22A5',color:'#FB923C',move:true,ball:false},
  handoff:{name:'Handoff',icon:'\u21CC',color:'#A78BFA',move:true,ball:true},
  shot:{name:'Shot',icon:'\u25CE',color:'#F87171',move:false,ball:false},
};
const uid=()=>Math.random().toString(36).substr(2,9);
const clamp=(v,a,b)=>Math.min(Math.max(v,a),b);
const deep=(o)=>JSON.parse(JSON.stringify(o));
const ACT_DUR=1.6; // seconds per action animation
const ACT_SPC=1.7; // seconds between sequential actions — tight (0.1s breath) to avoid stalls

class PlayCreator {
  constructor(container){
    this.el=container;
    this.players=[];this.initPlayers=[];this.actions=[];
    this.selPlayer=null;this.selAction=null;this.dragPlayer=null;
    this.drawing=false;this.curAction=null;
    this.mode='edit';this.playing=false;this.progress=0;this.duration=0;
    this.offTpl='5-out';this.defTpl='none';this.showTpl=true;
    this.saved=[];this.actTime=0;this.ballId=null;this.initBallId=null;this.showBall=false;
    this.editingCurve=null;this.draggingControl=false;
    this.parallelMode=false;this.parallelStart=null;
    this.positioningPhase=false;this.selectedStep=null;this.animId=null;
    this.fullCourt=false;
    this.shareUrl='';this.showShareModal=false;
    this.showConfirmModal=false;this.pendingLoad=null;
    this.showNewConfirm=false;this._currentPlayName=null;
    // Per-user localStorage namespace. The legacy key `coach_ai_plays`
    // was un-scoped, so Coach A's saved plays would appear in Coach B's
    // sidebar when they shared a browser. Migrate by deleting the legacy
    // key once and read/write only under the per-user key going forward.
    // For anonymous contexts (public share viewer), `_storageKey()`
    // returns null and we skip localStorage entirely.
    try{localStorage.removeItem('coach_ai_plays');}catch(e){}
    try{const k=this._storageKey();if(k){const s=localStorage.getItem(k);if(s)this.saved=JSON.parse(s);}}catch(e){}

    // ── On-screen debug overlay (toggle with ?debug=1 in URL) ──
    this._dbg=null;this._dbgLines=[];
    if(new URLSearchParams(window.location.search).get('debug')==='1'){
      const d=document.createElement('div');
      d.id='pcDebug';
      d.style.cssText='position:fixed;top:4px;left:4px;right:4px;z-index:99999;background:rgba(0,0,0,0.88);color:#0f0;font:10px/1.3 monospace;padding:6px 8px;max-height:110px;overflow-y:auto;pointer-events:none;white-space:pre-wrap;border-radius:6px';
      document.body.appendChild(d);
      this._dbg=d;
    }
    this._debugLog=(...args)=>{
      if(!this._dbg)return;
      const line=new Date().toISOString().slice(14,19)+' '+args.join(' ');
      this._dbgLines.unshift(line);
      if(this._dbgLines.length>10)this._dbgLines.pop();
      this._dbg.textContent=this._dbgLines.join('\n');
    };
    this._debugLog('init ok mode='+this.mode);

    this.render();

    // ── Document-level touch fallback (bulletproof layer) ──
    // Uses elementsFromPoint so we find .pp even if dispatch target is weird on iOS Safari.
    const docTouch=e=>{
      const t=e.touches&&e.touches[0];
      if(!t)return;
      const svg=this.el.querySelector('.pc-svg');
      if(!svg)return;
      const r=svg.getBoundingClientRect();
      if(t.clientX<r.left||t.clientX>r.right||t.clientY<r.top||t.clientY>r.bottom)return;
      let pp=null;
      const list=(document.elementsFromPoint?document.elementsFromPoint(t.clientX,t.clientY):[document.elementFromPoint(t.clientX,t.clientY)])||[];
      for(const el of list){if(el&&el.closest){const c=el.closest('.pp');if(c){pp=c;break;}}}
      this._debugLog('docTouch x='+Math.round(t.clientX)+' y='+Math.round(t.clientY)+' hit='+(list[0]&&list[0].tagName)+' pp='+(pp?pp.dataset.pid:'none'));
      if(!pp)return;
      const p=this.players.find(x=>x.id===pp.dataset.pid);
      if(!p)return;
      e.preventDefault();
      this._pd(e,p);
      this._svg();
    };
    document.addEventListener('touchstart',docTouch,{passive:false,capture:true});

    // Document-level touchmove/touchend fallback — ensures drag + release always catches
    const isDragging=()=>this.dragPlayer||this.drawing||this.draggingControl;
    let _moveCount=0;
    document.addEventListener('touchmove',e=>{
      if(!isDragging())return;
      const t=e.touches&&e.touches[0];if(!t)return;
      e.preventDefault();
      _moveCount++;
      if(_moveCount%5===1)this._debugLog('docMove '+_moveCount+' x='+Math.round(t.clientX)+' y='+Math.round(t.clientY));
      this._mv(e);
    },{passive:false,capture:true});
    const docEnd=()=>{if(isDragging()){this._debugLog('docEnd release moves='+_moveCount);_moveCount=0;this._up();document.body.classList.remove('pc-dragging');}};
    document.addEventListener('touchend',docEnd,{capture:true});
    document.addEventListener('touchcancel',docEnd,{capture:true});

    this._loadServerData();
    // Load from shared URL
    const params=new URLSearchParams(window.location.search);
    const shared=params.get('p');
    if(shared){try{const d=JSON.parse(decodeURIComponent(atob(shared)));this.loadPlay({o:d.o,d:d.d,i:d.i,a:d.a,b:d.b,fc:d.fc});window.history.replaceState({},'',window.location.pathname);}catch(e){Toast.error('Invalid play link');}}
    document.addEventListener('keydown',e=>{if(e.key==='Shift'){this.parallelMode=true;this._updateUI();}if(e.key==='z'&&(e.ctrlKey||e.metaKey)){e.preventDefault();this.undo();}});
    document.addEventListener('keyup',e=>{if(e.key==='Shift'){this.parallelMode=false;this._updateUI();}});
  }

  async _loadServerData(){
    try{
      const res=await fetch('/api/plays');
      const data=await res.json();
      if(data.success){
        const serverPlays=data.data||[];
        for(const sp of serverPlays){
          const local=this.saved.find(s=>s.name===sp.name);
          if(local){local.serverId=sp.id;}
          else{this.saved.push({id:sp.id.toString(),name:sp.name,o:sp.offense_template,d:sp.defense_template,i:sp.players||[],a:sp.actions||[],b:sp.ball_holder_id,serverId:sp.id});}
        }
        this._save();this.render();
      }
    }catch(e){}
  }

  _updateUI(){const b=this.el.querySelector('.pc-parallel-indicator');if(b)b.style.display=this.parallelMode?'inline-flex':'none';}
  _storageKey(){const m=document.querySelector('meta[name="np-user-id"]');const uid=m&&m.content;return uid?('coach_ai_plays_'+uid):null;}
  _save(){const k=this._storageKey();if(k){try{localStorage.setItem(k,JSON.stringify(this.saved));}catch(e){}}}
  _calcDur(){this.duration=this.actions.length>0?Math.max(...this.actions.map(a=>a.t+ACT_SPC)):0;}
  _getXY(e){const svg=this.el.querySelector('.pc-svg');if(!svg)return null;const pt=svg.createSVGPoint();pt.x=e.touches?.[0]?.clientX??e.clientX;pt.y=e.touches?.[0]?.clientY??e.clientY;const ctm=svg.getScreenCTM();if(!ctm)return null;const s=pt.matrixTransform(ctm.inverse());const maxY=this.fullCourt?168:84;return{x:clamp(s.x,4,96),y:clamp(s.y,4,maxY)};}
  _bez(sx,sy,cx,cy,ex,ey,t){const m=1-t;return{x:m*m*sx+2*m*t*cx+t*t*ex,y:m*m*sy+2*m*t*cy+t*t*ey};}

  _ppos(p){if(this.mode!=='play')return null;const i=this.initPlayers.find(z=>z.id===p.id);if(!i)return null;let x=i.x,y=i.y;for(const a of this.actions.filter(a=>a.pid===p.id&&ACTIONS[a.type]?.move)){const end=a.t+ACT_DUR;if(this.progress>=end){x=a.ex;y=a.ey;}else if(this.progress>a.t){const t=(this.progress-a.t)/ACT_DUR;const e=t<0.5?2*t*t:1-Math.pow(-2*t+2,2)/2;if(a.cx!==undefined){const ox=x-a.sx,oy=y-a.sy;const pt=this._bez(x,y,a.cx+ox,a.cy+oy,a.ex,a.ey,e);x=pt.x;y=pt.y;}else{const dx=a.ex-x,dy=a.ey-y;x=x+dx*e;y=y+dy*e;}}}return{x,y};}

  _bpos(){if(!this.initBallId)return null;if(this.mode==='edit'){const h=this.players.find(p=>p.id===this.ballId);return h?{x:h.x,y:h.y}:null;}let holder=this.initBallId,bx=null,by=null,mv=false;const pp=(id,at)=>{const i=this.initPlayers.find(p=>p.id===id);if(!i)return null;let x=i.x,y=i.y;for(const a of this.actions.filter(a=>a.pid===id&&ACTIONS[a.type]?.move&&a.t+ACT_DUR<=at)){x=a.ex;y=a.ey;}return{x,y};};for(const a of this.actions){const st=a.t,en=st+ACT_DUR;if(a.type==='pass'&&a.pid===holder){if(this.progress>=st&&this.progress<en){const t=(this.progress-st)/ACT_DUR;const f=pp(a.pid,st);if(f){bx=f.x+(a.ex-f.x)*t;by=f.y+(a.ey-f.y)*t;mv=true;}}else if(this.progress>=en){const o=this.players.filter(p=>p.type==='offense'&&p.id!==a.pid);let c=null,d=1e9;for(const p of o){const q=pp(p.id,st);if(q){const dd=Math.hypot(q.x-a.ex,q.y-a.ey);if(dd<d){d=dd;c=p;}}}if(c)holder=c.id;}}else if((a.type==='dribble'||a.type==='handoff')&&a.pid===holder){if(this.progress>=st&&this.progress<en){if(a.type==='dribble'){const dp=this.players.find(z=>z.id===a.pid);if(dp){const pos=this._ppos(dp);if(pos){bx=pos.x;by=pos.y;mv=true;}}}else{const t=(this.progress-st)/ACT_DUR;const e=t<0.5?2*t*t:1-Math.pow(-2*t+2,2)/2;const cx=a.cx??(a.sx+a.ex)/2,cy=a.cy??(a.sy+a.ey)/2;if(a.cx!==undefined){const pt=this._bez(a.sx,a.sy,cx,cy,a.ex,a.ey,e);bx=pt.x;by=pt.y;}else{const dx=a.ex-a.sx,dy=a.ey-a.sy,dd=Math.hypot(dx,dy);bx=a.sx+dx*e+(-dy/dd)*Math.sin(e*Math.PI)*dd*0.12;by=a.sy+dy*e+(dx/dd)*Math.sin(e*Math.PI)*dd*0.12;}mv=true;}}else if(this.progress>=en&&a.type==='handoff'){const o=this.players.filter(p=>p.type==='offense'&&p.id!==a.pid);let c=null,d=1e9;for(const p of o){const q=pp(p.id,en);if(q){const dd=Math.hypot(q.x-a.ex,q.y-a.ey);if(dd<d){d=dd;c=p;}}}if(c)holder=c.id;}}else if(a.type==='shot'&&a.pid===holder&&this.progress>=st){if(this.progress<en){const t=(this.progress-st)/ACT_DUR;bx=a.sx+(a.ex-a.sx)*t;by=a.sy+(a.ey-a.sy)*t-Math.sin(t*Math.PI)*15;mv=true;}else return{x:50,y:7};}}if(!mv){const hp=this.players.find(z=>z.id===holder);if(hp){const pos=this._ppos(hp);if(pos){bx=pos.x;by=pos.y;}else{const h2=pp(holder,this.progress);if(h2){bx=h2.x;by=h2.y;}}}else{const h2=pp(holder,this.progress);if(h2){bx=h2.x;by=h2.y;}}}return bx!==null?{x:bx,y:by}:null;}

  applyTpl(){const off=OFFENSE[this.offTpl]?.players||[];const def=DEFENSE[this.defTpl]?.players||[];const all=[...off.map(p=>({...p,id:uid()})),...def.map(p=>({...p,id:uid()}))];this.players=all;this.actions=[];this.actTime=0;this.ballId=null;this.initBallId=null;this.showTpl=false;this.parallelMode=false;this.parallelStart=null;this.editingCurve=null;if(this.offTpl==='empty'){this.initPlayers=[];this.positioningPhase=true;}else{this.initPlayers=deep(all);if(all.some(p=>p.type==='offense'))this.showBall=true;}this.render();}
  confirmPos(){this.initPlayers=deep(this.players);this.positioningPhase=false;if(this.players.some(p=>p.type==='offense'))this.showBall=true;this.render();}
  pickBall(id){this.ballId=id;this.initBallId=id;this.showBall=false;this.render();}
  selAct(type){this.selAction=this.selAction===type?null:type;this.selPlayer=null;this.editingCurve=null;this.render();}
  undo(){if(!this.actions.length)return;const l=this.actions[this.actions.length-1];if(ACTIONS[l.type]?.move){const p=this.players.find(x=>x.id===l.pid);if(p){p.x=l.sx;p.y=l.sy;}}if(l.type==='pass'||l.type==='handoff')this.ballId=l.pid;const rem=this.actions.slice(0,-1);const par=rem.some(a=>a.t===l.t);this.actions=rem;if(!par&&!this.parallelMode)this.actTime=Math.max(0,this.actTime-ACT_SPC);this._calcDur();this.render();}
  clearAct(){this.actions=[];this.actTime=0;this.players=deep(this.initPlayers);this.ballId=this.initBallId;this.parallelMode=false;this.parallelStart=null;this.editingCurve=null;this.selectedStep=null;this._calcDur();this.render();}

  async savePlay(){const n=await NpDialog.prompt('Enter play name:', { title: 'Save Play', icon: 'sports_basketball', placeholder: 'Play name...' });if(!n||!n.trim())return;const name=n.trim();const entry={id:uid(),name:name,o:this.offTpl,d:this.defTpl,i:this.initPlayers,a:this.actions,b:this.initBallId,fc:this.fullCourt?1:0};
    try{
      const res=await fetch('/api/plays',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,offense_template:this.offTpl,defense_template:this.defTpl,players:this.initPlayers,actions:this.actions,ball_holder_id:this.initBallId})});
      const data=await res.json();
      if(data.success&&data.data?.id){entry.serverId=data.data.id;Toast.success('Play saved!');}
    }catch(e){Toast.error('Could not save to server');}
    this.saved.push(entry);this._save();this._currentPlayName=name;this.render();
  }

  loadPlay(pl){this.offTpl=pl.o;this.defTpl=pl.d;this.fullCourt=!!pl.fc;this.initPlayers=pl.i;this.initBallId=pl.b;const ps=deep(pl.i);let h=pl.b;for(const a of pl.a){if(ACTIONS[a.type]?.move){const i=ps.findIndex(x=>x.id===a.pid);if(i>=0){ps[i].x=a.ex;ps[i].y=a.ey;}}if((a.type==='pass'||a.type==='handoff')&&a.pid===h){const o=ps.filter(p=>p.type==='offense'&&p.id!==a.pid);let c=null,d=1e9;for(const p of o){const dd=Math.hypot(p.x-a.ex,p.y-a.ey);if(dd<d){d=dd;c=p;}}if(c)h=c.id;}}this.ballId=h;this.players=ps;this.actions=pl.a;this.actTime=pl.a.length>0?Math.max(...pl.a.map(a=>a.t))+ACT_SPC:0;this.mode='edit';this.showTpl=false;this._calcDur();this.showConfirmModal=false;this.pendingLoad=null;this._currentPlayName=pl.name||null;this.render();}
  async delPlay(id){
    const entry=this.saved.find(s=>s.id===id);
    if(entry?.serverId){try{await fetch('/api/plays/'+entry.serverId,{method:'DELETE'});}catch(e){}}
    this.saved=this.saved.filter(s=>s.id!==id);this._save();this.render();
  }

  async share(){if(!this.actions.length)return;const d={o:this.offTpl,d:this.defTpl,i:this.initPlayers,a:this.actions,b:this.initBallId,fc:this.fullCourt?1:0,n:this._currentPlayName||null};try{const res=await fetch('/api/plays/share',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});const data=await res.json();this.shareUrl=data.url||'';} catch(e){this.shareUrl=window.location.origin+'/plays?p='+btoa(encodeURIComponent(JSON.stringify(d)));}this.showShareModal=true;this.render();}
  shareWhatsApp(){if(!this.shareUrl)return;const text=encodeURIComponent('Check out this basketball play: '+this.shareUrl);window.open('https://wa.me/?text='+text,'_blank');}
  shareEmail(){if(!this.shareUrl)return;const subject=encodeURIComponent('Basketball Play');const body=encodeURIComponent('Check out this play:\n'+this.shareUrl);window.location.href='mailto:?subject='+subject+'&body='+body;}

  newPlay(){if(this.actions.length>0||this.initPlayers.length>0){this.showNewConfirm=true;this.render();}else{this._resetToNew();}}
  _resetToNew(){this.players=[];this.initPlayers=[];this.actions=[];this.selPlayer=null;this.selAction=null;this.mode='edit';this.playing=false;this.progress=0;this.duration=0;this.offTpl='5-out';this.defTpl='none';this.fullCourt=false;this.showTpl=true;this.actTime=0;this.ballId=null;this.initBallId=null;this.showBall=false;this.editingCurve=null;this.parallelMode=false;this.parallelStart=null;this.positioningPhase=false;this.selectedStep=null;this.showNewConfirm=false;this._currentPlayName=null;this.render();}
  async newPlaySaveAndNew(){this.showNewConfirm=false;await this.savePlay();this._resetToNew();}

  // Animation
  play(){if(this.playing)return;this.mode='play';this.playing=true;this.progress=0;this._calcDur();this._as=performance.now();this._aloop();}
  _aloop(){if(!this.playing)return;const t=(performance.now()-this._as)/1000;if(t>=this.duration){this.progress=this.duration;this.playing=false;this._svg();this._ctrl();return;}this.progress=t;this._svg();this._ctrl();this.animId=requestAnimationFrame(()=>this._aloop());}
  pause(){this.playing=false;if(this.animId)cancelAnimationFrame(this.animId);}
  stop(){this.pause();this.progress=0;this.mode='edit';this.render();}

  // Events
  _pd(e,p){e.stopPropagation();if(this.mode!=='edit'){this._debugLog&&this._debugLog('_pd skip mode='+this.mode);return;}const xy=this._getXY(e);if(!xy){this._debugLog&&this._debugLog('_pd skip no-xy');return;}this._markUsedOnce();if(this.positioningPhase){this.dragPlayer=p;this.selPlayer=p.id;document.body.classList.add('pc-dragging');this._debugLog&&this._debugLog('_pd pos-phase drag='+p.id);return;}if(this.selAction){this.drawing=true;this.curAction={id:uid(),type:this.selAction,pid:p.id,sx:p.x,sy:p.y,ex:xy.x,ey:xy.y};document.body.classList.add('pc-dragging');this._debugLog&&this._debugLog('_pd drawing='+this.selAction);}else{this.dragPlayer=p;this.selPlayer=p.id;document.body.classList.add('pc-dragging');this._debugLog&&this._debugLog('_pd drag='+p.id);}this.editingCurve=null;}

  _markUsedOnce(){
    // Fire once per page load — the server endpoint is idempotent per team+event.
    if(this._playCreatorUsedSent)return;
    this._playCreatorUsedSent=true;
    try{
      fetch('/api/onboarding/mark-event',{
        method:'POST',
        headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
        credentials:'same-origin',
        body:JSON.stringify({event:'play_creator_used'})
      }).catch(function(){});
    }catch(e){}
  }
  _mv(e){const xy=this._getXY(e);if(!xy)return;if(this.drawing&&this.curAction){this.curAction.ex=xy.x;this.curAction.ey=xy.y;this._svg();}else if(this.dragPlayer){const p=this.players.find(x=>x.id===this.dragPlayer.id);if(p){p.x=xy.x;p.y=xy.y;if(this.actions.length===0){const ip=this.initPlayers.find(x=>x.id===this.dragPlayer.id);if(ip){ip.x=xy.x;ip.y=xy.y;}}this._svg();}}else if(this.draggingControl&&this.editingCurve){const a=this.actions.find(x=>x.id===this.editingCurve);if(a){a.cx=xy.x;a.cy=xy.y;this._svg();}}}
  _up(){if(this.drawing&&this.curAction){const d=Math.hypot(this.curAction.ex-this.curAction.sx,this.curAction.ey-this.curAction.sy);if(d>5){const ts=this.parallelMode&&this.parallelStart!==null?this.parallelStart:this.actTime;const na={...this.curAction,t:ts};const ex=this.actions.find(a=>a.t===ts&&a.pid===this.curAction.pid);if(!(this.parallelMode&&ex)){this.actions.push(na);this.editingCurve=na.id;if(!this.parallelMode)this.actTime+=ACT_SPC;if(ACTIONS[this.curAction.type]?.move){const p=this.players.find(x=>x.id===this.curAction.pid);if(p){p.x=this.curAction.ex;p.y=this.curAction.ey;}}if((this.curAction.type==='pass'||this.curAction.type==='handoff')&&this.curAction.pid===this.ballId){const o=this.players.filter(p=>p.type==='offense'&&p.id!==this.curAction.pid);let c=null,cd=1e9;for(const p of o){const dd=Math.hypot(p.x-this.curAction.ex,p.y-this.curAction.ey);if(dd<cd){cd=dd;c=p;}}if(c)this.ballId=c.id;}}}}this.drawing=false;this.curAction=null;this.dragPlayer=null;this.draggingControl=false;this._calcDur();this.render();}

  _pn(pid){const p=this.players.find(x=>x.id===pid)||this.initPlayers.find(x=>x.id===pid);return p?p.number:'?';}
  _steps(){const g={};this.actions.forEach(a=>{const k=a.t.toFixed(1);if(!g[k])g[k]=[];g[k].push(a);});return Object.entries(g).sort((a,b)=>parseFloat(a[0])-parseFloat(b[0])).map(([t,acts],i)=>({time:parseFloat(t),actions:acts,step:i+1}));}

  render(){this.el.innerHTML=this._html();this._bindEv();this._svg();}
  _ctrl(){const f=this.el.querySelector('.pc-pf');const th=this.el.querySelector('.pc-pt');const tm=this.el.querySelector('.pc-tm');if(f&&this.duration>0){const p=(this.progress/this.duration*100)+'%';f.style.width=p;if(th)th.style.left=p;}if(tm)tm.textContent=this.progress.toFixed(1)+'s / '+this.duration.toFixed(1)+'s';}

  _html(){
    const steps=this._steps();
    const isEdit=this.mode==='edit';
    const hasActions=this.actions.length>0;

    // Top toolbar
    let html='<div class="pc"><div class="pc-toolbar"><div class="pc-toolbar-group">'
      +'<button class="btn btn-sm '+(isEdit?'btn-primary':'btn-secondary')+'" data-a="edit"><span class="material-symbols-outlined" style="font-size:15px">edit</span> Edit</button>'
      +'<button class="btn btn-sm '+(this.mode==='play'?'btn-primary':'btn-secondary')+'" data-a="pmode"><span class="material-symbols-outlined" style="font-size:15px">play_arrow</span> Play</button>'
      +'<button class="btn btn-sm btn-secondary" data-a="newPlay"><span class="material-symbols-outlined" style="font-size:15px">add</span> New</button>'
      +'<span class="pc-parallel-indicator pc-parallel-badge" style="display:'+(this.parallelMode?'inline-flex':'none')+'">Parallel</span>'
      +'</div><div class="pc-toolbar-group">'
      +'<button class="btn btn-sm btn-primary" data-a="save"'+(hasActions?'':' disabled')+'><span class="material-symbols-outlined" style="font-size:15px">save</span> Save</button>'
      +'<button class="btn btn-sm btn-secondary" data-a="share"'+(hasActions?'':' disabled')+'><span class="material-symbols-outlined" style="font-size:15px">share</span> Share</button>'
      +'</div></div>';

    // Body: canvas + sidebar
    const vbH=this.fullCourt?172:88;
    html+='<div class="pc-body"><div class="pc-canvas-wrap">'
      +'<svg class="pc-svg" viewBox="0 0 100 '+vbH+'" preserveAspectRatio="xMidYMid meet"></svg>';

    // Left toolbar - Actions (edit mode, not positioning)
    if(isEdit&&!this.positioningPhase){
      html+='<div class="pc-left-toolbar">';
      Object.entries(ACTIONS).forEach(([k,v])=>{
        html+='<button class="pc-tool-btn '+(this.selAction===k?'active':'')+'" data-sa="'+k+'" title="'+v.name+'">'
          +'<span style="color:'+v.color+';font-size:15px">'+v.icon+'</span>'
          +'<span class="pc-tool-label" style="color:'+v.color+'">'+v.name+'</span></button>';
      });
      html+='</div>';
    }

    // Right toolbar - Controls
    if(isEdit){
      html+='<div class="pc-right-toolbar">';
      if(this.positioningPhase){
        html+='<button class="pc-tool-btn" data-a="cpos" title="Confirm positions" style="color:var(--success)"><span class="material-symbols-outlined" style="font-size:15px">check_circle</span><span class="pc-tool-label">Confirm</span></button>';
      }else{
        html+='<button class="pc-tool-btn'+(this.parallelMode?' active':'')+'" data-a="toggleParallel" title="Parallel" style="'+(this.parallelMode?'color:#FBBF24;border-color:#FBBF24':'')+'"><span class="material-symbols-outlined" style="font-size:15px">stacks</span><span class="pc-tool-label" style="'+(this.parallelMode?'color:#FBBF24':'')+'">Parallel</span></button>'
          +'<button class="pc-tool-btn" data-a="undo" title="Undo (Ctrl+Z)"'+(hasActions?'':' disabled')+'><span class="material-symbols-outlined" style="font-size:15px">undo</span><span class="pc-tool-label">Undo</span></button>'
          +'<button class="pc-tool-btn" data-a="clr" title="Clear all"'+(hasActions?'':' disabled')+'><span class="material-symbols-outlined" style="font-size:15px">delete_sweep</span><span class="pc-tool-label">Clear</span></button>'
          +'<div class="pc-tool-sep"></div>'
          +'<button class="pc-tool-btn" data-a="tpl" title="Select Formation"><span class="material-symbols-outlined" style="font-size:15px">dashboard</span><span class="pc-tool-label">Formation</span></button>'
          +'<button class="pc-tool-btn" data-a="play" title="Play"'+(hasActions?'':' disabled')+'><span class="material-symbols-outlined" style="font-size:15px;color:var(--success)">play_arrow</span><span class="pc-tool-label" style="color:var(--success)">Play</span></button>';
      }
      html+='</div>';
    }
    if(this.mode==='play'){
      html+='<div class="pc-right-toolbar">'
        +'<button class="pc-tool-btn" data-a="edit" title="Edit"><span class="material-symbols-outlined" style="font-size:15px">edit</span><span class="pc-tool-label">Edit</span></button>'
        +'<button class="pc-tool-btn" data-a="'+(this.playing?'pause':'play')+'" title="'+(this.playing?'Pause':'Play')+'"><span class="material-symbols-outlined" style="font-size:15px">'+(this.playing?'pause':'play_arrow')+'</span><span class="pc-tool-label">'+(this.playing?'Pause':'Play')+'</span></button>'
        +'<button class="pc-tool-btn" data-a="stop" title="Stop"><span class="material-symbols-outlined" style="font-size:15px">replay</span><span class="pc-tool-label">Reset</span></button>'
        +'</div>';
    }

    // Status indicators
    if(isEdit&&!this.positioningPhase&&this.selAction){
      html+='<div class="pc-status pc-status-action">'+ACTIONS[this.selAction].icon+' Click a player and drag to add '+ACTIONS[this.selAction].name+'</div>';
    }
    if(isEdit&&this.parallelMode&&!this.positioningPhase){
      html+='<div class="pc-status pc-status-parallel" style="bottom:calc(var(--sp-3) + 28px)">Parallel mode: actions happen simultaneously</div>';
    }
    if(isEdit&&this.positioningPhase){
      html+='<div class="pc-status pc-status-position">Drag players to position them, then click \u2713</div>';
    }
    if(isEdit&&this.editingCurve){
      html+='<div class="pc-status pc-status-curve">Drag the control point to curve the line</div>';
    }

    // Template/ball overlays
    html+=(this.showTpl?this._tplHtml():'')+(this.showBall?this._ballHtml():'');
    html+='</div>'; // close pc-canvas-wrap

    // Right sidebar - timeline + saved
    html+='<div class="pc-sidebar"><div class="pc-sidebar-section"><div class="pc-sidebar-title">Timeline ('+steps.length+' steps)</div>'
      +(steps.length?steps.map(s=>'<div class="pc-timeline-step '+(this.selectedStep===s.time?'active':'')+'" data-st="'+s.time+'"><div style="font-weight:700;margin-bottom:2px">Step '+s.step+' <span style="color:var(--text-muted);font-weight:400">'+s.time.toFixed(1)+'s</span></div>'+s.actions.map(a=>'<div class="pc-timeline-action"><span style="color:'+(ACTIONS[a.type]?.color||'#fff')+'">'+(ACTIONS[a.type]?.icon||'')+'</span><span>#'+this._pn(a.pid)+'</span><span style="color:var(--text-muted)">'+(ACTIONS[a.type]?.name||a.type)+'</span><button class="btn-icon pc-act-btn" style="margin-left:auto" data-ec="'+a.id+'"><span class="material-symbols-outlined" style="font-size:14px">timeline</span></button><button class="btn-icon pc-act-btn" data-da="'+a.id+'"><span class="material-symbols-outlined" style="font-size:14px">close</span></button></div>').join('')+'</div>').join(''):'<div style="padding:var(--sp-2);font-size:var(--text-xs);color:var(--text-muted)">No actions yet. Select an action tool and drag on a player.</div>')
      +'</div><div class="pc-sidebar-section"><div class="pc-sidebar-title">Saved Plays ('+this.saved.length+')</div>'
      +(this.saved.length?this.saved.map(s=>'<div class="pc-saved-item"><span data-lp="'+s.id+'" style="cursor:pointer;flex:1">'+s.name+'</span><button class="btn-icon pc-act-btn" data-dp="'+s.id+'"><span class="material-symbols-outlined" style="font-size:14px">delete</span></button></div>').join(''):'<div style="padding:var(--sp-2);font-size:var(--text-xs);color:var(--text-muted)">No saved plays yet.</div>')
      +'</div></div></div>'; // close pc-sidebar, pc-body

    // Playback controls bar
    html+='<div class="pc-controls">'
      +'<button class="btn-icon" data-a="'+(this.playing?'pause':'play')+'"><span class="material-symbols-outlined">'+(this.playing?'pause':'play_arrow')+'</span></button>'
      +'<button class="btn-icon" data-a="stop"><span class="material-symbols-outlined">stop</span></button>'
      +'<div class="pc-progress" id="pcProg"><div class="pc-pf pc-progress-fill" style="width:'+(this.duration>0?this.progress/this.duration*100:0)+'%"></div><div class="pc-pt pc-progress-thumb" style="left:'+(this.duration>0?this.progress/this.duration*100:0)+'%"></div></div>'
      +'<span class="pc-tm" style="min-width:60px;text-align:end;font-size:var(--text-xs);color:var(--text-muted)">'+this.progress.toFixed(1)+'s / '+this.duration.toFixed(1)+'s</span>'
      +'</div>';

    // Modals
    if(this.showShareModal) html+=this._shareHtml();
    if(this.showConfirmModal) html+=this._confirmHtml();
    if(this.showNewConfirm) html+=this._newConfirmHtml();

    html+='</div>'; // close pc
    return html;
  }

  _tplHtml(){return '<div class="pc-modal"><h2>Select Formation</h2>'
    +'<div class="pc-sidebar-title">Court</div>'
    +'<div class="pc-tpl-grid" style="grid-template-columns:repeat(2,1fr);margin-bottom:var(--sp-4)">'
    +'<button class="pc-tpl-btn '+(!this.fullCourt?'active':'')+'" data-court="half"><span class="material-symbols-outlined" style="font-size:20px;vertical-align:middle">vertical_align_bottom</span> Half Court</button>'
    +'<button class="pc-tpl-btn '+(this.fullCourt?'active':'')+'" data-court="full"><span class="material-symbols-outlined" style="font-size:20px;vertical-align:middle">height</span> Full Court</button>'
    +'</div>'
    +'<div class="pc-sidebar-title">Offense</div><div class="pc-tpl-grid">'+Object.entries(OFFENSE).map(([k,v])=>'<button class="pc-tpl-btn '+(this.offTpl===k?'active':'')+'" data-ot="'+k+'">'+v.name+'</button>').join('')+'</div><div class="pc-sidebar-title">Defense</div><div class="pc-tpl-grid" style="grid-template-columns:repeat(2,1fr)">'+Object.entries(DEFENSE).map(([k,v])=>'<button class="pc-tpl-btn '+(this.defTpl===k?'active':'')+'" data-dt="'+k+'">'+v.name+'</button>').join('')+'</div><button class="btn btn-primary btn-lg" data-a="apply" style="margin-top:var(--sp-4);width:100%"><span class="material-symbols-outlined">check</span> Apply Formation</button></div>';}
  _ballHtml(){const o=this.players.filter(p=>p.type==='offense');return '<div class="pc-modal"><h2>Who has the ball?</h2><div class="pc-tpl-grid">'+o.map(p=>'<button class="pc-tpl-btn" data-pb="'+p.id+'">#'+p.number+'</button>').join('')+'</div></div>';}

  _shareHtml(){return '<div class="modal-overlay active" style="z-index:50"><div class="modal" style="max-width:500px"><div class="modal-header"><h3 class="modal-title">Share Play</h3><button class="modal-close" data-a="closeShare"><span class="material-symbols-outlined">close</span></button></div><div class="modal-body"><p style="margin-bottom:var(--sp-3);color:var(--text-secondary)">Copy the link or share via WhatsApp / Email:</p><div style="display:flex;gap:var(--sp-2);margin-bottom:var(--sp-3)"><input class="input" id="shareUrlInput" value="'+this.shareUrl+'" readonly style="flex:1;font-size:var(--text-xs)"/><button class="btn btn-primary" data-a="copyShare"><span class="material-symbols-outlined" style="font-size:15px">content_copy</span></button></div><div style="display:flex;gap:var(--sp-2)"><button class="btn btn-secondary" data-a="shareWA" style="flex:1"><span class="material-symbols-outlined" style="font-size:15px">chat</span> WhatsApp</button><button class="btn btn-secondary" data-a="shareEmail" style="flex:1"><span class="material-symbols-outlined" style="font-size:15px">mail</span> Email</button></div></div></div></div>';}
  _confirmHtml(){return '<div class="modal-overlay active" style="z-index:50"><div class="modal" style="max-width:400px"><div class="modal-header"><h3 class="modal-title">Load Play</h3><button class="modal-close" data-a="cancelLoad"><span class="material-symbols-outlined">close</span></button></div><div class="modal-body"><p style="color:var(--text-secondary)">Loading a play will replace your current work. Continue?</p></div><div class="modal-footer"><button class="btn btn-secondary" data-a="cancelLoad">Stay</button><button class="btn btn-primary" data-a="confirmLoad">Load Play</button></div></div></div>';}
  _newConfirmHtml(){return '<div class="modal-overlay active" style="z-index:50"><div class="modal" style="max-width:420px"><div class="modal-header"><h3 class="modal-title">New Play</h3><button class="modal-close" data-a="newStay"><span class="material-symbols-outlined">close</span></button></div><div class="modal-body"><p style="color:var(--text-secondary)"><span class="material-symbols-outlined" style="font-size:20px;vertical-align:middle;color:var(--warning)">warning</span> You have unsaved changes. What would you like to do?</p></div><div class="modal-footer" style="gap:var(--sp-2)"><button class="btn btn-secondary" data-a="newStay">Stay</button><button class="btn btn-danger" data-a="newDelete">Discard</button><button class="btn btn-primary" data-a="newSave"><span class="material-symbols-outlined" style="font-size:16px">save</span> Save & New</button></div></div></div>';}

  _bindEv(){
    const svg=this.el.querySelector('.pc-svg');
    if(svg){svg.addEventListener('mousemove',e=>this._mv(e));svg.addEventListener('mouseup',()=>this._up());svg.addEventListener('mouseleave',()=>this._up());svg.addEventListener('touchstart',e=>{e.preventDefault();},{passive:false});svg.addEventListener('touchmove',e=>{e.preventDefault();this._mv(e);},{passive:false});svg.addEventListener('touchend',e=>{e.preventDefault();this._up();},{passive:false});svg.addEventListener('touchcancel',e=>{e.preventDefault();this._up();},{passive:false});}
    this.el.querySelectorAll('[data-a]').forEach(b=>{b.addEventListener('click',()=>{const a=b.dataset.a;
      if(a==='edit'){this.pause();this.mode='edit';this.render();}
      else if(a==='pmode'){this.mode='play';this.progress=0;this.render();}
      else if(a==='tpl'){this.showTpl=true;this.render();}
      else if(a==='undo')this.undo();
      else if(a==='clr')this.clearAct();
      else if(a==='save')this.savePlay();
      else if(a==='apply')this.applyTpl();
      else if(a==='cpos')this.confirmPos();
      else if(a==='play')this.play();
      else if(a==='pause')this.pause();
      else if(a==='stop')this.stop();
      else if(a==='share')this.share();
      else if(a==='toggleParallel'){if(!this.parallelMode){this.parallelMode=true;this.parallelStart=this.actTime;}else{this.parallelMode=false;this.actTime+=ACT_SPC;this.parallelStart=null;}this.render();}
      else if(a==='closeShare'){this.showShareModal=false;this.render();}
      else if(a==='copyShare'){const inp=this.el.querySelector('#shareUrlInput');if(inp){navigator.clipboard.writeText(inp.value).then(()=>Toast.success('Link copied!')).catch(()=>{inp.select();document.execCommand('copy');Toast.success('Link copied!');});}}
      else if(a==='shareWA')this.shareWhatsApp();
      else if(a==='shareEmail')this.shareEmail();
      else if(a==='confirmLoad'){if(this.pendingLoad)this.loadPlay(this.pendingLoad);}
      else if(a==='cancelLoad'){this.pendingLoad=null;this.showConfirmModal=false;this.render();}
      else if(a==='newPlay')this.newPlay();
      else if(a==='newStay'){this.showNewConfirm=false;this.render();}
      else if(a==='newDelete')this._resetToNew();
      else if(a==='newSave')this.newPlaySaveAndNew();
    });});
    this.el.querySelectorAll('[data-sa]').forEach(b=>{b.addEventListener('click',()=>this.selAct(b.dataset.sa));});
    this.el.querySelectorAll('[data-court]').forEach(b=>{b.addEventListener('click',()=>{this.fullCourt=b.dataset.court==='full';this.render();});});
    this.el.querySelectorAll('[data-ot]').forEach(b=>{b.addEventListener('click',()=>{this.offTpl=b.dataset.ot;this.render();});});
    this.el.querySelectorAll('[data-dt]').forEach(b=>{b.addEventListener('click',()=>{this.defTpl=b.dataset.dt;this.render();});});
    this.el.querySelectorAll('[data-pb]').forEach(b=>{b.addEventListener('click',()=>this.pickBall(b.dataset.pb));});
    this.el.querySelectorAll('[data-st]').forEach(el=>{el.addEventListener('click',()=>{const st=parseFloat(el.dataset.st);this.selectedStep=this.selectedStep===st?null:st;this.render();});});
    this.el.querySelectorAll('[data-ec]').forEach(b=>{b.addEventListener('click',e=>{e.stopPropagation();const id=b.dataset.ec;this.editingCurve=this.editingCurve===id?null:id;this._svg();});});
    this.el.querySelectorAll('[data-da]').forEach(b=>{b.addEventListener('click',e=>{e.stopPropagation();this.actions=this.actions.filter(a=>a.id!==b.dataset.da);const ps=deep(this.initPlayers);for(const a of this.actions){if(ACTIONS[a.type]?.move){const i=ps.findIndex(x=>x.id===a.pid);if(i>=0){ps[i].x=a.ex;ps[i].y=a.ey;}}}this.players=ps;this._calcDur();this.render();});});
    this.el.querySelectorAll('[data-lp]').forEach(el=>{el.addEventListener('click',()=>{const p=this.saved.find(s=>s.id===el.dataset.lp);if(!p)return;if(this.actions.length>0){this.pendingLoad=p;this.showConfirmModal=true;this.render();}else{this.loadPlay(p);}});});
    this.el.querySelectorAll('[data-dp]').forEach(b=>{b.addEventListener('click',e=>{e.stopPropagation();this.delPlay(b.dataset.dp);});});
    const prog=this.el.querySelector('#pcProg');
    if(prog){const progHandler=e=>{const r=prog.getBoundingClientRect();const cx=e.touches?e.touches[0].clientX:e.clientX;this.progress=((cx-r.left)/r.width)*this.duration;this.mode='play';this._svg();this._ctrl();};prog.addEventListener('click',progHandler);prog.addEventListener('touchstart',e=>{e.preventDefault();progHandler(e);},{passive:false});}
  }

  _svg(){
    const svg=this.el.querySelector('.pc-svg');if(!svg)return;
    const fc=this.fullCourt;
    const courtH=fc?172:88;
    const boundH=fc?168:84;
    let h='<defs>'
      +'<pattern id="woodGrain" patternUnits="userSpaceOnUse" width="8" height="'+courtH+'">'
      +'<rect width="8" height="'+courtH+'" fill="#C17F4E"/>'
      +'<rect x="0" y="0" width="8" height="'+courtH+'" fill="#B8733F" opacity="0.3"/>'
      +'<line x1="0" y1="0" x2="0" y2="'+courtH+'" stroke="#A05A2C" stroke-width="0.3" opacity="0.4"/>'
      +'<line x1="4" y1="0" x2="4" y2="'+courtH+'" stroke="#D4956A" stroke-width="0.2" opacity="0.3"/>'
      +'<rect x="1" y="5" width="6" height="0.5" fill="#A05A2C" opacity="0.15"/>'
      +'<rect x="2" y="20" width="4" height="0.3" fill="#D4956A" opacity="0.1"/>'
      +'<rect x="0" y="35" width="8" height="0.4" fill="#A05A2C" opacity="0.12"/>'
      +'<rect x="1" y="50" width="5" height="0.3" fill="#D4956A" opacity="0.08"/>'
      +'<rect x="3" y="65" width="3" height="0.5" fill="#A05A2C" opacity="0.1"/>'
      +'<rect x="0" y="80" width="7" height="0.3" fill="#D4956A" opacity="0.12"/>'
      +'<line x1="2" y1="0" x2="2" y2="'+courtH+'" stroke="#A05A2C" stroke-width="0.15" opacity="0.2"/>'
      +'<line x1="6" y1="0" x2="6" y2="'+courtH+'" stroke="#D4956A" stroke-width="0.15" opacity="0.15"/>'
      +'</pattern>'
      +'<linearGradient id="paintGradient" x1="0%" y1="0%" x2="0%" y2="100%">'
      +'<stop offset="0%" stop-color="#1E3A5F" stop-opacity="0.15"/>'
      +'<stop offset="100%" stop-color="#1E3A5F" stop-opacity="0.05"/>'
      +'</linearGradient>'
      +'<radialGradient id="ballGrad" cx="35%" cy="30%" r="70%">'
      +'<stop offset="0%" stop-color="#FFB270"/>'
      +'<stop offset="55%" stop-color="#F97316"/>'
      +'<stop offset="100%" stop-color="#B45309"/>'
      +'</radialGradient></defs>';

    // Court floor
    h+='<rect width="100" height="'+courtH+'" fill="#C8844A"/>';
    h+='<rect width="100" height="'+courtH+'" fill="url(#woodGrain)"/>';
    h+='<rect x="0" y="0" width="100" height="44" fill="url(#paintGradient)" opacity="0.3"/>';
    h+='<rect x="31" y="2" width="38" height="38" fill="#1E3A5F" opacity="0.08"/>';
    // Court boundary
    h+='<rect x="2" y="2" width="96" height="'+boundH+'" fill="none" stroke="#FFFFFF" stroke-width="0.8"/>';

    // === TOP HALF (basket at top) ===
    h+='<path d="M 7 2 L 7 16 C 7 38 25 57 50 57 C 75 57 93 38 93 16 L 93 2" fill="none" stroke="#FFFFFF" stroke-width="0.6" stroke-linejoin="round"/>';
    h+='<rect x="31" y="2" width="38" height="38" fill="none" stroke="#FFFFFF" stroke-width="0.6"/>';
    h+='<line x1="29" y1="14" x2="31" y2="14" stroke="#FFFFFF" stroke-width="0.4"/>';
    h+='<line x1="29" y1="22" x2="31" y2="22" stroke="#FFFFFF" stroke-width="0.4"/>';
    h+='<line x1="29" y1="30" x2="31" y2="30" stroke="#FFFFFF" stroke-width="0.4"/>';
    h+='<line x1="69" y1="14" x2="71" y2="14" stroke="#FFFFFF" stroke-width="0.4"/>';
    h+='<line x1="69" y1="22" x2="71" y2="22" stroke="#FFFFFF" stroke-width="0.4"/>';
    h+='<line x1="69" y1="30" x2="71" y2="30" stroke="#FFFFFF" stroke-width="0.4"/>';
    h+='<circle cx="50" cy="40" r="12" fill="none" stroke="#FFFFFF" stroke-width="0.5"/>';
    h+='<path d="M 38 40 A 12 12 0 0 0 62 40" fill="none" stroke="#FFFFFF" stroke-width="0.5" stroke-dasharray="2,2"/>';
    h+='<path d="M 42 2 A 8 8 0 0 0 58 2" fill="none" stroke="#FFFFFF" stroke-width="0.5"/>';
    h+='<rect x="44" y="2.5" width="12" height="1" fill="#FFFFFF" opacity="0.9"/>';
    h+='<rect x="44" y="2.5" width="12" height="1" fill="none" stroke="#333" stroke-width="0.2"/>';
    h+='<circle cx="50" cy="6" r="1.5" fill="none" stroke="#FF5722" stroke-width="0.5"/>';
    h+='<line x1="50" y1="3.5" x2="50" y2="4.5" stroke="#888" stroke-width="0.3"/>';
    h+='<path d="M 48.5 6 Q 50 9 51.5 6" fill="none" stroke="#FFFFFF" stroke-width="0.2" opacity="0.5"/>';

    if(fc){
      // === CENTER LINE + CENTER CIRCLE ===
      const mid=courtH/2+2; // y=88
      h+='<line x1="2" y1="'+mid+'" x2="98" y2="'+mid+'" stroke="#FFFFFF" stroke-width="0.6"/>';
      h+='<circle cx="50" cy="'+mid+'" r="12" fill="none" stroke="#FFFFFF" stroke-width="0.5"/>';
      h+='<circle cx="50" cy="'+mid+'" r="3" fill="none" stroke="#FFFFFF" stroke-width="0.5"/>';

      // === BOTTOM HALF (mirrored — basket at bottom) ===
      const bY=courtH; // bottom of court boundary = 2 + boundH = 170
      // Paint fill
      h+='<rect x="0" y="'+(bY-44)+'" width="100" height="44" fill="url(#paintGradient)" opacity="0.3"/>';
      h+='<rect x="31" y="'+(bY-40)+'" width="38" height="38" fill="#1E3A5F" opacity="0.08"/>';
      // 3-point arc (mirrored)
      h+='<path d="M 7 '+bY+' L 7 '+(bY-14)+' C 7 '+(bY-36)+' 25 '+(bY-55)+' 50 '+(bY-55)+' C 75 '+(bY-55)+' 93 '+(bY-36)+' 93 '+(bY-14)+' L 93 '+bY+'" fill="none" stroke="#FFFFFF" stroke-width="0.6" stroke-linejoin="round"/>';
      // Lane
      h+='<rect x="31" y="'+(bY-40)+'" width="38" height="38" fill="none" stroke="#FFFFFF" stroke-width="0.6"/>';
      // FT ticks (left)
      h+='<line x1="29" y1="'+(bY-14)+'" x2="31" y2="'+(bY-14)+'" stroke="#FFFFFF" stroke-width="0.4"/>';
      h+='<line x1="29" y1="'+(bY-22)+'" x2="31" y2="'+(bY-22)+'" stroke="#FFFFFF" stroke-width="0.4"/>';
      h+='<line x1="29" y1="'+(bY-30)+'" x2="31" y2="'+(bY-30)+'" stroke="#FFFFFF" stroke-width="0.4"/>';
      // FT ticks (right)
      h+='<line x1="69" y1="'+(bY-14)+'" x2="71" y2="'+(bY-14)+'" stroke="#FFFFFF" stroke-width="0.4"/>';
      h+='<line x1="69" y1="'+(bY-22)+'" x2="71" y2="'+(bY-22)+'" stroke="#FFFFFF" stroke-width="0.4"/>';
      h+='<line x1="69" y1="'+(bY-30)+'" x2="71" y2="'+(bY-30)+'" stroke="#FFFFFF" stroke-width="0.4"/>';
      // FT circle
      h+='<circle cx="50" cy="'+(bY-40)+'" r="12" fill="none" stroke="#FFFFFF" stroke-width="0.5"/>';
      h+='<path d="M 38 '+(bY-40)+' A 12 12 0 0 1 62 '+(bY-40)+'" fill="none" stroke="#FFFFFF" stroke-width="0.5" stroke-dasharray="2,2"/>';
      // FT semicircle at baseline
      h+='<path d="M 42 '+bY+' A 8 8 0 0 1 58 '+bY+'" fill="none" stroke="#FFFFFF" stroke-width="0.5"/>';
      // Backboard
      h+='<rect x="44" y="'+(bY-1.5)+'" width="12" height="1" fill="#FFFFFF" opacity="0.9"/>';
      h+='<rect x="44" y="'+(bY-1.5)+'" width="12" height="1" fill="none" stroke="#333" stroke-width="0.2"/>';
      // Rim
      h+='<circle cx="50" cy="'+(bY-4)+'" r="1.5" fill="none" stroke="#FF5722" stroke-width="0.5"/>';
      // Net hint
      h+='<line x1="50" y1="'+(bY-2.5)+'" x2="50" y2="'+(bY-3.5)+'" stroke="#888" stroke-width="0.3"/>';
      h+='<path d="M 48.5 '+(bY-4)+' Q 50 '+(bY-7)+' 51.5 '+(bY-4)+'" fill="none" stroke="#FFFFFF" stroke-width="0.2" opacity="0.5"/>';
    }else{
      // Half court: simple half-court line + opposing basket arc
      h+='<line x1="2" y1="86" x2="98" y2="86" stroke="#FFFFFF" stroke-width="0.4" opacity="0.6"/>';
      h+='<path d="M 38 86 A 12 12 0 0 1 62 86" fill="none" stroke="#FFFFFF" stroke-width="0.4" opacity="0.6"/>';
    }

    // Action lines
    if(this.mode==='play'){
      const previewT=0.4;
      this.actions.forEach(a=>{
        const end=a.t+ACT_DUR;
        if(this.progress>=end)return;
        if(this.progress<a.t-previewT)return;
        if(this.progress<a.t){h+=this._line(a,1,0.7,false);return;}
        const rawP=Math.min(1,(this.progress-a.t)/ACT_DUR);
        const e=rawP<0.5?2*rawP*rawP:1-Math.pow(-2*rawP+2,2)/2;
        const hc=a.cx!==undefined&&a.cy!==undefined;
        let cx,cy;
        if(hc){const pt=this._bez(a.sx,a.sy,a.cx,a.cy,a.ex,a.ey,e);cx=pt.x;cy=pt.y;}
        else if(ACTIONS[a.type]?.move){const dx=a.ex-a.sx,dy=a.ey-a.sy,d=Math.hypot(dx,dy);cx=a.sx+dx*e+(-dy/d)*Math.sin(e*Math.PI)*d*0.12;cy=a.sy+dy*e+(dx/d)*Math.sin(e*Math.PI)*d*0.12;}
        else{cx=a.sx+(a.ex-a.sx)*e;cy=a.sy+(a.ey-a.sy)*e;}
        const rem={...a,sx:cx,sy:cy,cx:undefined,cy:undefined};
        h+=this._line(rem,1,0.8-rawP*0.5,false);
      });
    }else{
      this.actions.forEach((a,idx)=>{const op=this.actions.length>3&&idx<this.actions.length-3?0.25:1;h+=this._line(a,1,op,a.id===this.editingCurve);});
    }
    if(this.curAction)h+=this._line(this.curAction,1,0.6,false);

    // Players
    this.players.forEach(p=>{
      const pos=this.mode==='play'?(this._ppos(p)||p):p;
      const f=p.type==='offense'?'#3B82F6':'#EF4444';
      const s=p.id===this.selPlayer;
      const isBall=this.mode==='edit'&&p.id===this.ballId;
      h+='<g class="pp" data-pid="'+p.id+'" style="cursor:'+(this.mode==='edit'?'grab':'default')+'">';
      if(s){
        // Selection highlight — visible but not overgrown.
        h+='<circle cx="'+pos.x+'" cy="'+pos.y+'" r="6" fill="none" stroke="#FBBF24" stroke-width="0.6" opacity="0.9" pointer-events="none"/>';
      }
      if(isBall) h+='<circle cx="'+pos.x+'" cy="'+pos.y+'" r="5.5" fill="none" stroke="#F97316" stroke-width="0.8" opacity="0.5" pointer-events="none"/>';
      // Visible player circle IS the clickable target — matches the visible footprint.
      h+='<circle cx="'+pos.x+'" cy="'+pos.y+'" r="4.5" fill="'+f+'" stroke="'+(s?'#FBBF24':isBall?'#F97316':'#fff')+'" stroke-width="'+(s||isBall?'1':'0.5')+'"/>';
      h+='<text x="'+pos.x+'" y="'+pos.y+'" text-anchor="middle" dominant-baseline="central" fill="#fff" font-size="3" font-weight="700" font-family="Space Grotesk,sans-serif" pointer-events="none">'+p.number+'</text></g>';
    });

    // Basketball — realistic render: gradient body + 4 seams + highlight
    const bp=this._bpos();
    if(bp){
      const bx=bp.x, by=bp.y-6;
      const r=3;
      // Ground shadow
      h+='<ellipse cx="'+bx+'" cy="'+(bp.y-1.2)+'" rx="'+(r*0.85)+'" ry="0.55" fill="rgba(0,0,0,0.32)"/>';
      // Body with radial gradient
      h+='<circle cx="'+bx+'" cy="'+by+'" r="'+r+'" fill="url(#ballGrad)" stroke="#7C2D12" stroke-width="0.3"/>';
      // Main horizontal seam (curves slightly down — equator)
      h+='<path d="M '+(bx-r)+' '+by+' Q '+bx+' '+(by+0.35)+' '+(bx+r)+' '+by+'" fill="none" stroke="#7C2D12" stroke-width="0.35" stroke-linecap="round"/>';
      // Main vertical seam (slight curve to the right — prime meridian)
      h+='<path d="M '+bx+' '+(by-r)+' Q '+(bx+0.35)+' '+by+' '+bx+' '+(by+r)+'" fill="none" stroke="#7C2D12" stroke-width="0.35" stroke-linecap="round"/>';
      // Left side curve
      h+='<path d="M '+(bx-r*0.88)+' '+(by-r*0.55)+' Q '+(bx-r*0.15)+' '+by+' '+(bx-r*0.88)+' '+(by+r*0.55)+'" fill="none" stroke="#7C2D12" stroke-width="0.3" stroke-linecap="round"/>';
      // Right side curve
      h+='<path d="M '+(bx+r*0.88)+' '+(by-r*0.55)+' Q '+(bx+r*0.15)+' '+by+' '+(bx+r*0.88)+' '+(by+r*0.55)+'" fill="none" stroke="#7C2D12" stroke-width="0.3" stroke-linecap="round"/>';
      // Soft highlight top-left for 3D feel
      h+='<ellipse cx="'+(bx-r*0.38)+'" cy="'+(by-r*0.4)+'" rx="'+(r*0.3)+'" ry="'+(r*0.18)+'" fill="rgba(255,220,180,0.45)"/>';
    }

    svg.innerHTML=h;
    // Event delegation: ONE listener on SVG that finds .pp via closest().
    if(!svg._ppDelegated){
      const pickPlayer=e=>{
        const t=e.target;
        const pp=t&&t.closest?t.closest('.pp'):null;
        if(!pp)return null;
        return this.players.find(x=>x.id===pp.dataset.pid)||null;
      };
      svg.addEventListener('mousedown',e=>{const p=pickPlayer(e);this._debugLog('svgMouse pp='+(p?p.id:'none'));if(p){this._pd(e,p);this._svg();}});
      svg.addEventListener('touchstart',e=>{
        const p=pickPlayer(e);
        this._debugLog('svgTouch tgt='+(e.target&&e.target.tagName)+' pp='+(p?p.id:'none'));
        if(p){e.preventDefault();e.stopPropagation();this._pd(e,p);this._svg();}
      },{passive:false,capture:true});
      svg._ppDelegated=true;
    }
    svg.querySelectorAll('.pc-cp').forEach(c=>{c.addEventListener('mousedown',e=>{e.stopPropagation();this.draggingControl=true;this.editingCurve=c.dataset.ca;});c.addEventListener('touchstart',e=>{e.preventDefault();e.stopPropagation();this.draggingControl=true;this.editingCurve=c.dataset.ca;},{passive:false});});
    svg.querySelectorAll('.pc-curve-toggle').forEach(c=>{c.addEventListener('mousedown',e=>{e.stopPropagation();this.editingCurve=c.dataset.ct;this._svg();});c.addEventListener('touchstart',e=>{e.preventDefault();e.stopPropagation();this.editingCurve=c.dataset.ct;this._svg();},{passive:false});});
  }

  _line(a,p,op,ed){const{type,sx,sy,ex,ey,cx,cy}=a;const dx=ex-sx,dy=ey-sy,dist=Math.hypot(dx,dy);if(dist<2)return'';const col=ACTIONS[type]?.color||'#fff';const hc=cx!==undefined&&cy!==undefined;const cX=cx??(sx+ex)/2,cY=cy??(sy+ey)/2;let h='<g opacity="'+op+'" style="cursor:pointer">';
    if(type==='screen'){const ang=Math.atan2(ey-sy,ex-sx),pa=ang+Math.PI/2;const ep=hc?this._bez(sx,sy,cX,cY,ex,ey,p):{x:sx+dx*p,y:sy+dy*p};if(hc)h+='<path d="M '+sx+' '+sy+' Q '+cX+' '+cY+' '+ep.x+' '+ep.y+'" fill="none" stroke="'+col+'" stroke-width="0.6"/>';else h+='<line x1="'+sx+'" y1="'+sy+'" x2="'+ep.x+'" y2="'+ep.y+'" stroke="'+col+'" stroke-width="0.6"/>';h+='<line x1="'+(ep.x-3*Math.cos(pa))+'" y1="'+(ep.y-3*Math.sin(pa))+'" x2="'+(ep.x+3*Math.cos(pa))+'" y2="'+(ep.y+3*Math.sin(pa))+'" stroke="'+col+'" stroke-width="1" stroke-linecap="round"/>';}
    else if(type==='shot'){const ep={x:sx+dx*p,y:sy+dy*p};h+='<line x1="'+sx+'" y1="'+sy+'" x2="'+ep.x+'" y2="'+ep.y+'" stroke="'+col+'" stroke-width="0.5" stroke-dasharray="1.5,0.8"/>';if(p===1)h+='<circle cx="'+ep.x+'" cy="'+ep.y+'" r="1.5" fill="none" stroke="'+col+'" stroke-width="0.4"/>';}
    else{const ep=hc?this._bez(sx,sy,cX,cY,ex,ey,p):{x:sx+dx*p,y:sy+dy*p};const da=type==='pass'?'stroke-dasharray="2,1"':type==='dribble'?'stroke-dasharray="1,0.5"':'';if(hc)h+='<path d="M '+sx+' '+sy+' Q '+cX+' '+cY+' '+ep.x+' '+ep.y+'" fill="none" stroke="'+col+'" stroke-width="0.6" '+da+'/>';else h+='<line x1="'+sx+'" y1="'+sy+'" x2="'+ep.x+'" y2="'+ep.y+'" stroke="'+col+'" stroke-width="0.6" '+da+'/>';if(p===1){const ang=hc?Math.atan2(ep.y-cY,ep.x-cX):Math.atan2(dy,dx);h+='<polygon points="'+ep.x+','+ep.y+' '+(ep.x-2*Math.cos(ang-0.4))+','+(ep.y-2*Math.sin(ang-0.4))+' '+(ep.x-2*Math.cos(ang+0.4))+','+(ep.y-2*Math.sin(ang+0.4))+'" fill="'+col+'"/>';}}
    if(ed){const cpx=cx??(sx+ex)/2,cpy=cy??(sy+ey)/2-dist*0.15;h+='<circle cx="'+cpx+'" cy="'+cpy+'" r="1.5" fill="#fff" stroke="'+col+'" stroke-width="0.4" class="pc-cp" data-ca="'+a.id+'" style="cursor:grab"/>';h+='<line x1="'+sx+'" y1="'+sy+'" x2="'+cpx+'" y2="'+cpy+'" stroke="#fff" stroke-width="0.2" stroke-dasharray="1,1" opacity="0.4"/>';h+='<line x1="'+cpx+'" y1="'+cpy+'" x2="'+ex+'" y2="'+ey+'" stroke="#fff" stroke-width="0.2" stroke-dasharray="1,1" opacity="0.4"/>';}
    else if(this.mode==='edit'&&p===1&&type!=='shot'){const mx=hc?this._bez(sx,sy,cX,cY,ex,ey,0.5).x:(sx+ex)/2,my=hc?this._bez(sx,sy,cX,cY,ex,ey,0.5).y:(sy+ey)/2;h+='<circle cx="'+mx+'" cy="'+my+'" r="2" fill="rgba(0,0,0,0.5)" stroke="#fff" stroke-width="0.3" class="pc-curve-toggle" data-ct="'+a.id+'" style="cursor:pointer"/>';h+='<text x="'+mx+'" y="'+(my+0.5)+'" text-anchor="middle" dominant-baseline="central" fill="#fff" font-size="2.5" class="pc-curve-toggle" data-ct="'+a.id+'" style="cursor:pointer;pointer-events:auto">\u27F3</text>';}
    h+='</g>';return h;
  }
}

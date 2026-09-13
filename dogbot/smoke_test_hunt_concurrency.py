"""Cross-connection concurrency/integration stress tests for naval Hunt."""
import asyncio, os, random, sqlite3, sys, tempfile
from datetime import datetime, timezone

HERE=os.path.dirname(os.path.abspath(__file__)); ROOT=os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT,"smoke_stubs")); sys.path.insert(0,ROOT)
import dogbot.database as db_module

class Cur:
    def __init__(self,c): self.c=c
    @property
    def rowcount(self): return self.c.rowcount
    @property
    def lastrowid(self): return self.c.lastrowid
    async def fetchone(self): return self.c.fetchone()
    async def fetchall(self): return self.c.fetchall()
class Conn:
    def __init__(self,c): object.__setattr__(self,"_c",c)
    def __setattr__(self,n,v):
        if n=="row_factory": self._c.row_factory=v
        else: object.__setattr__(self,n,v)
    async def execute(self,sql,p=()): return Cur(self._c.execute(sql,p))
    async def commit(self): self._c.commit()
    async def rollback(self): self._c.rollback()
    async def close(self): self._c.close()
async def connect(path,*a,**k): return Conn(sqlite3.connect(path,check_same_thread=False))
class Aio: Row=sqlite3.Row; connect=staticmethod(connect)
db_module.aiosqlite=Aio

passed=failed=0
def check(label,ok):
    global passed,failed
    if ok: passed+=1; print("OK  ",label)
    else: failed+=1; print("FAIL",label)

async def setup():
    fd,path=tempfile.mkstemp(prefix="hunt-xconn-",suffix=".db"); os.close(fd)
    a=db_module.UserFactsDB(path); b=db_module.UserFactsDB(path)
    await a.setup(); await b.setup()
    return a,b,path

async def add_hunt(db,hid,room):
    await db.conn.execute("INSERT INTO hunts(id,room_jid,status,start_at,registration_opens_at) VALUES(?,?,?,?,?)",
      (hid,room,"active","2026-01-01T00:00:00+00:00","2026-01-01T00:00:00+00:00")); await db.conn.commit()

SHIP={"name":"Ship","class_name":"frigate","hp":10000,"max_hp":10000,"sails":100,"crew_morale":70,
      "crew_count":10,"crew_hp":1000,"crew_max_hp":1000,"registered_crew_count":10,"distance":50,"speed":40,"heading":0,"fire_level":0}

async def test_delta(a,b):
    await add_hunt(a,1,"r1"); sid=await a.hunt_create_ship(1,"player",SHIP)
    async def one(i):
        db=a if i%2==0 else b
        return await db.hunt_apply_ship_delta(sid,hp_delta=-7,sails_delta=-2,morale_delta=-1,distance_delta=1)
    rows=await asyncio.gather(*(one(i) for i in range(100)))
    s=await a.hunt_get_ship(sid)
    check("100 cross-connection HP deltas keep all damage",s["hp"]==9300)
    check("100 cross-connection sail deltas keep all damage",s["sails"]==0)
    check("cross-connection morale deltas are atomic",s["crew_morale"]==0)
    check("distance is clamped atomically",s["distance"]==100)
    check("all delta calls return state",all(rows))

async def test_gate(a,b):
    await add_hunt(a,2,"r2")
    res=await asyncio.gather(*((a if i%2==0 else b).hunt_try_claim_global_action(2,f"u{i}",25) for i in range(30)))
    check("30 cross-connection crew actions admit exactly one",sum(x[0] for x in res)==1)
    check("29 cross-connection crew actions are rejected",sum(not x[0] for x in res)==29)

async def test_weapon(a,b):
    await add_hunt(a,3,"r3"); sid=await a.hunt_create_ship(3,"player",SHIP)
    wid=await a.hunt_create_weapon(3,sid,"Gun","bow",100,80,1,30)
    now=datetime.now(timezone.utc).isoformat(timespec="seconds")
    res=await asyncio.gather(*((a if i%2==0 else b).hunt_claim_weapon_shot(wid,now,30) for i in range(20)))
    cur=await a.conn.execute("SELECT ammo FROM hunt_weapons WHERE id=?",(wid,)); row=await cur.fetchone()
    check("20 cross-connection shot claims consume exactly one ammo",sum(res)==1)
    check("weapon ammo never becomes negative",row["ammo"]==0)

async def test_boarding(a,b):
    await add_hunt(a,4,"r4")
    aa=await a.hunt_create_ship(4,"player",dict(SHIP,name="A")); bb=await a.hunt_create_ship(4,"player",dict(SHIP,name="B")); target=await a.hunt_create_ship(4,"enemy",dict(SHIP,name="Target",distance=20))
    res=await asyncio.gather(a.hunt_try_claim_boarding(4,aa,target),b.hunt_try_claim_boarding(4,bb,target))
    cur=await a.conn.execute("SELECT COUNT(*) n FROM hunt_boardings WHERE hunt_id=4 AND defender_ship_id=? AND status='in_progress'",(target,)); row=await cur.fetchone()
    check("different attackers: exactly one boarding wins across connections",sum(x[0] for x in res)==1)
    check("boarding loser gets in-progress reason",[x[2] for x in res if not x[0]]==["boarding_in_progress"])
    check("DB has exactly one active boarding per target",row["n"]==1)

async def test_crew(a,b):
    await add_hunt(a,5,"r5"); sid=await a.hunt_create_ship(5,"enemy",dict(SHIP,name="Crewship",hp=1000,max_hp=1000,crew_count=5,crew_hp=500,crew_max_hp=500))
    for i in range(5): await a.hunt_create_npc(5,sid,f"N{i}","sailor",100)
    await asyncio.gather(*((a if i%2==0 else b).hunt_apply_crew_damage(5,sid,10) for i in range(10)))
    s=await a.hunt_get_ship_crew_summary(5,sid)
    check("100 cross-connection crew damage is fully applied",s["crew_hp"]==400)
    check("crew count remains consistent",s["crew_count"]==5)

async def main():
    for fn in (test_delta,test_gate,test_weapon,test_boarding,test_crew):
        a,b,path=await setup()
        try: await fn(a,b)
        finally: await a.conn.close(); await b.conn.close(); os.unlink(path)
    print(f"\n{passed} passed, {failed} failed"); raise SystemExit(bool(failed))
if __name__=="__main__": asyncio.run(main())

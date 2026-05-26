#!/usr/bin/env python3
"""plot_session.py — visual analytics for a G2 controller-tracking capture.

Loads the telemetry streams (manifest-driven parquet) and plots, per controller:
  1. position vs time — OPTICAL (PnP) vs IMU-dead-reckon vs FUSED, w.r.t. the HMD/tracking frame
  2. orientation vs time — OPTICAL vs gyro-dead-reckon vs FUSED (shows mirror-flips)
  3. pipeline-health timeline — IMU / blobbed-frames / accepted-poses / fusion / lock-loss rates
  4. 3D trajectory (healthy window)
Problem instances are shaded/marked: optical dropout gaps, the table-rest tail, divergence excursions.

Usage: python3 plot_session.py <capture_dir>   (dir containing telemetry/)
Outputs PNGs + an interactive plotly HTML into <capture_dir>/analysis/.
"""
import os, sys, numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import pyarrow.parquet as pq

GAP_S = 0.30          # optical-dropout threshold (a fusion-event gap longer than this = IMU-only)
FAR_M = 2.0           # a controller pos beyond this from origin is physically implausible (divergence)
G_WORLD = np.array([0.0, -9.80665, 0.0])   # OpenXR: +Y up, gravity points down
DEV = {1: "LEFT", 2: "RIGHT"}

def load(T, name):
    t = pq.read_table(f"{T}/{name}.parquet")
    return pd.DataFrame({c: t.column(c).to_numpy() for c in t.column_names})

def quat_mul(a, b):  # xyzw
    ax,ay,az,aw=a; bx,by,bz,bw=b
    return np.array([aw*bx+ax*bw+ay*bz-az*by, aw*by-ax*bz+ay*bw+az*bx,
                     aw*bz+ax*by-ay*bx+az*bw, aw*bw-ax*bx-ay*by-az*bz])
def quat_rot(q, v):  # rotate v by quaternion q (xyzw)
    x,y,z,w=q; u=np.array([x,y,z]); return v+2*w*np.cross(u,v)+2*np.cross(u,np.cross(u,v))
def quat_from_rotvec(r):
    a=np.linalg.norm(r)
    if a<1e-9: return np.array([0,0,0,1.0])
    s=np.sin(a/2)/a; return np.array([r[0]*s,r[1]*s,r[2]*s,np.cos(a/2)])
def quat_angle(q1,q2):
    d=np.abs(np.sum(q1*q2,axis=1)); return np.degrees(2*np.arccos(np.clip(d,0,1)))

def imu_deadreckon(imu_dev, anchors):
    """Strapdown dead-reckon RESET at each optical anchor, integrated only through the gap after it.
    anchors: list of (t_s, pos(3), quat(4 xyzw), vel0(3)). Returns dict gap_idx -> (t[],p[3xN],q[4xN])."""
    ti = imu_dev["t_mono_ns"].to_numpy()/1e9
    acc = imu_dev[["ax","ay","az"]].to_numpy(); gyr = imu_dev[["gx","gy","gz"]].to_numpy()
    out = {}
    for gi,(t0,p0,q0,v0) in enumerate(anchors):
        sel = np.where((ti>=t0)&(ti<=t0+3.0))[0]   # cap at 3s of dead-reckon for plotting
        if len(sel)<2: continue
        p=p0.copy(); v=v0.copy(); q=q0.copy(); P=[p.copy()]; Q=[q.copy()]; TS=[ti[sel[0]]]
        for k in range(1,len(sel)):
            dt=ti[sel[k]]-ti[sel[k-1]]
            if dt<=0 or dt>0.25: dt=min(max(dt,0),0.005)
            q=quat_mul(q, quat_from_rotvec(gyr[sel[k]]*dt)); q/=np.linalg.norm(q)
            a_world=quat_rot(q, acc[sel[k]])+G_WORLD     # specific force -> world accel
            p=p+v*dt+0.5*a_world*dt*dt; v=v+a_world*dt
            P.append(p.copy()); Q.append(q.copy()); TS.append(ti[sel[k]])
        out[gi]=(np.array(TS),np.array(P),np.array(Q))
    return out

def main(cap):
    T=f"{cap}/telemetry"; out=f"{cap}/analysis"; os.makedirs(out,exist_ok=True)
    imu=load(T,"imu"); fus=load(T,"fusion"); pa=load(T,"pose_attempt"); fr=load(T,"frame"); ev=load(T,"event")
    t0=imu["t_mono_ns"].min()
    for d in (imu,fus,pa,fr,ev): d["t"]=(d["t_mono_ns"]-t0)/1e9
    dur=imu["t"].max()
    # table-rest tail = where controller IMU rate collapses (held ~250Hz -> idle when set down)
    rL=np.histogram(imu[imu.device_id==1].t,bins=np.arange(0,dur+1,1))[0]
    table_t = next((i for i in range(len(rL)-3) if i>20 and rL[i:i+3].mean()<rL[:i].mean()*0.4), None)

    # ---------- per-controller position & orientation ----------
    for dev in (1,2):
        f=fus[fus.device_id==dev].sort_values("t").reset_index(drop=True)
        if len(f)==0: continue
        opt=f[["opt_px","opt_py","opt_pz"]].to_numpy(); prd=f[["pred_px","pred_py","pred_pz"]].to_numpy()
        # optical dropout gaps
        dt=np.diff(f.t.to_numpy()); gaps=[(f.t[i],f.t[i+1]) for i in np.where(dt>GAP_S)[0]]
        # anchors for IMU dead-reckon: start of each gap, using optical pose + velocity from fused finite-diff
        anchors=[]
        ft=f.t.to_numpy()
        for i in np.where(dt>GAP_S)[0]:
            v0=(prd[i]-prd[max(i-1,0)])/max(ft[i]-ft[max(i-1,0)],1e-2) if i>0 else np.zeros(3)
            v0=np.clip(v0,-3,3)
            anchors.append((ft[i], opt[i].copy(), f[["opt_qx","opt_qy","opt_qz","opt_qw"]].to_numpy()[i], v0))
        dr=imu_deadreckon(imu[imu.device_id==dev], anchors)

        fig,ax=plt.subplots(4,1,figsize=(16,12),sharex=True)
        labs=["X (right)","Y (up)","Z (fwd)"]
        for j in range(3):
            ax[j].scatter(f.t,opt[:,j],s=6,c="#2ca02c",alpha=.5,label="optical (PnP)")
            ax[j].plot(f.t,prd[:,j],c="#1f77b4",lw=1.1,label="fused (ESKF)")
            for gi,(ts,P,Q) in dr.items():
                ax[j].plot(ts,P[:,j],c="#ff7f0e",lw=.8,alpha=.7)
            ax[j].set_ylabel(labs[j]+" [m]"); ax[j].set_ylim(-FAR_M,FAR_M); ax[j].grid(alpha=.3)
        # |pos| log panel to show divergence
        ax[3].semilogy(f.t,np.linalg.norm(prd,axis=1),c="#1f77b4",lw=1,label="|fused|")
        ax[3].semilogy(f.t,np.linalg.norm(opt,axis=1),".",ms=3,c="#2ca02c",alpha=.5,label="|optical|")
        ax[3].axhline(FAR_M,c="r",ls="--",lw=.8); ax[3].set_ylabel("|pos| [m] (log)"); ax[3].set_xlabel("time [s]"); ax[3].grid(alpha=.3)
        for a in ax:
            for (g0,g1) in gaps: a.axvspan(g0,g1,color="grey",alpha=.12)
            if table_t: a.axvspan(table_t,dur,color="red",alpha=.06)
        ax[0].plot([],[],c="#ff7f0e",label="IMU dead-reckon (per-gap, reconstructed)")
        ax[0].legend(loc="upper left",ncol=4,fontsize=8)
        ax[0].set_title(f"{DEV[dev]} controller — position w.r.t. HMD frame  |  grey=optical-dropout gap (>{GAP_S}s)  red=controllers on table")
        fig.tight_layout(); fig.savefig(f"{out}/pos_{DEV[dev].lower()}.png",dpi=110); plt.close(fig)

        # orientation: optical vs gyro-dead-reckon vs fused, as angle from session start quat
        qo=f[["opt_qx","opt_qy","opt_qz","opt_qw"]].to_numpy(); qp=f[["pred_qx","pred_qy","pred_qz","pred_qw"]].to_numpy()
        ref=qp[0]
        ang_o=quat_angle(qo, np.tile(ref,(len(qo),1))); ang_p=quat_angle(qp, np.tile(ref,(len(qp),1)))
        fig,ax=plt.subplots(2,1,figsize=(16,7),sharex=True)
        ax[0].plot(f.t,ang_p,c="#1f77b4",lw=1,label="fused angle-from-start")
        ax[0].scatter(f.t,ang_o,s=6,c="#2ca02c",alpha=.5,label="optical angle-from-start")
        for gi,(ts,P,Q) in dr.items():
            ax[0].plot(ts,quat_angle(Q,np.tile(ref,(len(Q),1))),c="#ff7f0e",lw=.8,alpha=.7)
        ax[0].set_ylabel("orientation [deg]"); ax[0].grid(alpha=.3); ax[0].legend(loc="upper left",fontsize=8)
        ax[0].set_title(f"{DEV[dev]} controller — orientation: optical vs gyro-dead-reckon (orange) vs fused")
        # step (frame-to-frame) jumps = flips
        ax[1].plot(f.t[1:],quat_angle(qo[:-1],qo[1:]),".",ms=3,c="#2ca02c",alpha=.5,label="optical step")
        ax[1].plot(f.t[1:],quat_angle(qp[:-1],qp[1:]),c="#1f77b4",lw=.8,label="fused step")
        ax[1].axhline(75,c="r",ls="--",lw=.8,label="flip-reject 75°"); ax[1].set_ylabel("Δorientation/step [deg]"); ax[1].set_xlabel("time [s]"); ax[1].grid(alpha=.3); ax[1].legend(loc="upper left",fontsize=8)
        for a in ax:
            for (g0,g1) in gaps: a.axvspan(g0,g1,color="grey",alpha=.12)
            if table_t: a.axvspan(table_t,dur,color="red",alpha=.06)
        fig.tight_layout(); fig.savefig(f"{out}/ori_{DEV[dev].lower()}.png",dpi=110); plt.close(fig)

    # ---------- pipeline-health timeline ----------
    bins=np.arange(0,dur+1,1)
    def rate(df,mask=None):
        s=df.t.to_numpy() if mask is None else df.t.to_numpy()[mask.to_numpy()]
        return np.histogram(s,bins=bins)[0]
    fig,ax=plt.subplots(5,1,figsize=(16,11),sharex=True)
    ax[0].plot(bins[:-1],rate(imu,imu.device_id==1),label="L"); ax[0].plot(bins[:-1],rate(imu,imu.device_id==2),label="R")
    ax[0].set_ylabel("IMU [Hz]"); ax[0].legend(fontsize=8); ax[0].set_title("Pipeline health per second — where the data dies")
    ax[1].plot(bins[:-1],rate(fr),c="grey",label="frames"); ax[1].plot(bins[:-1],rate(fr,fr.n_blobs>0),c="k",label="frames w/ blobs")
    ax[1].set_ylabel("frames"); ax[1].legend(fontsize=8)
    ax[2].plot(bins[:-1],rate(pa),c="orange",label="pose attempts"); ax[2].plot(bins[:-1],rate(pa,pa.outcome!=0),c="green",label="accepted")
    ax[2].set_ylabel("PnP poses"); ax[2].legend(fontsize=8)
    ax[3].plot(bins[:-1],rate(fus,fus.device_id==1),label="fusion L"); ax[3].plot(bins[:-1],rate(fus,fus.device_id==2),label="fusion R")
    ax[3].set_ylabel("fusion folds"); ax[3].legend(fontsize=8)
    ax[4].plot(bins[:-1],rate(ev,ev.event_type==0),c="red",label="lock_lost"); ax[4].plot(bins[:-1],rate(ev,ev.event_type==2),c="purple",label="recover")
    ax[4].set_ylabel("events"); ax[4].set_xlabel("time [s]"); ax[4].legend(fontsize=8)
    for a in ax:
        a.grid(alpha=.3)
        if table_t: a.axvspan(table_t,dur,color="red",alpha=.06)
    fig.tight_layout(); fig.savefig(f"{out}/pipeline_health.png",dpi=110); plt.close(fig)

    # ---------- 3D trajectory (healthy window, clipped) ----------
    from mpl_toolkits.mplot3d import Axes3D  # noqa
    fig=plt.figure(figsize=(15,7))
    for n,dev in enumerate((1,2)):
        f=fus[(fus.device_id==dev)&(fus.t< (table_t or dur))].copy()
        opt=f[["opt_px","opt_py","opt_pz"]].to_numpy(); prd=f[["pred_px","pred_py","pred_pz"]].to_numpy()
        keep=np.linalg.norm(opt,axis=1)<FAR_M
        a=fig.add_subplot(1,2,n+1,projection="3d")
        a.scatter(opt[keep,0],opt[keep,1],opt[keep,2],s=4,c="#2ca02c",alpha=.3,label="optical")
        a.plot(prd[keep,0],prd[keep,1],prd[keep,2],c="#1f77b4",lw=.6,label="fused")
        a.set_title(f"{DEV[dev]} 3D (|pos|<{FAR_M}m)"); a.set_xlabel("X"); a.set_ylabel("Y"); a.set_zlabel("Z"); a.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(f"{out}/traj3d.png",dpi=110); plt.close(fig)

    print("wrote:", ", ".join(sorted(os.listdir(out))))
    print(f"table-rest detected from t={table_t}s" if table_t else "no table-rest tail detected")

if __name__=="__main__":
    main(sys.argv[1] if len(sys.argv)>1 else os.environ.get("G2_CAP","."))

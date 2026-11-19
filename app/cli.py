from __future__ import annotations
import argparse
import getpass
import secrets
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import select, delete
from .auth import passwords
from .db import ROOT, SessionLocal
from .models import LoginSession, Membership, Store, User

def main():
    parser=argparse.ArgumentParser(description="MultiStore account administration")
    sub=parser.add_subparsers(dest="command",required=True)
    first=sub.add_parser("init",help="Create first account and store")
    first.add_argument("--username",required=True);first.add_argument("--store",required=True)
    first.add_argument("--generate-password",action="store_true")
    user=sub.add_parser("add-user",help="Invite an account to an existing store")
    user.add_argument("--username",required=True);user.add_argument("--store-id",required=True,type=int)
    reset=sub.add_parser("reset-password",help="Reset an account password and revoke its sessions")
    reset.add_argument("--username",required=True)
    new_store=sub.add_parser("add-store",help="Create a store and grant an existing user")
    new_store.add_argument("--name",required=True);new_store.add_argument("--username",required=True)
    args=parser.parse_args()
    cfg=Config(str(ROOT/"alembic.ini"));cfg.set_main_option("script_location",str(ROOT/"migrations"));command.upgrade(cfg,"head")
    with SessionLocal() as db:
        if args.command=="reset-password":
            u=db.scalar(select(User).where(User.username==args.username))
            if not u:raise SystemExit("User not found")
            password=getpass.getpass("New password (at least 12 characters): ")
            if len(password)<12:raise SystemExit("Password too short")
            if password!=getpass.getpass("Repeat password: "):raise SystemExit("Passwords differ")
            u.password_hash=passwords.hash(password);db.execute(delete(LoginSession).where(LoginSession.user_id==u.id));db.commit()
            print("Password reset and sessions revoked");return
        if args.command=="add-store":
            u=db.scalar(select(User).where(User.username==args.username))
            if not u: raise SystemExit("User not found")
            store=Store(name=args.name);db.add(store);db.flush();db.add(Membership(user_id=u.id,store_id=store.id));db.commit();print(f"Created store {store.id}");return
        if db.scalar(select(User).where(User.username==args.username)): raise SystemExit("User already exists")
        if args.command=="init" and db.scalar(select(User.id)): raise SystemExit("Initial account already exists")
        if args.command=="add-user" and not db.get(Store,args.store_id): raise SystemExit("Store not found")
        generated=getattr(args,"generate_password",False)
        path=ROOT/"private"/"initial-login.txt"
        if generated and path.exists(): raise SystemExit("Login file exists; account not created")
        password=secrets.token_urlsafe(18) if generated else getpass.getpass("Password (at least 12 characters): ")
        if len(password)<12: raise SystemExit("Password too short")
        if not generated and password!=getpass.getpass("Repeat password: "): raise SystemExit("Passwords differ")
        u=User(username=args.username,password_hash=passwords.hash(password),is_admin=args.command=="init");db.add(u);db.flush()
        if args.command=="init":
            store=Store(name=args.store);db.add(store);db.flush();store_id=store.id
        else: store_id=args.store_id
        db.add(Membership(user_id=u.id,store_id=store_id));db.commit()
        if generated:
            path.parent.mkdir(exist_ok=True)
            path.write_text(f"username={args.username}\npassword={password}\n",encoding="utf-8")
            print("Account created. Initial credentials saved to private/initial-login.txt")
        else: print("Account created")
if __name__=="__main__": main()

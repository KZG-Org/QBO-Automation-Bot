from __future__ import annotations

import os
import re
import time
import random
import json
import requests
import pandas as pd
from config import settings
from gspread.utils import rowcol_to_a1
from gspread.exceptions import APIError

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import gspread

def _extract_sheet_id(url_or_id: str) -> str:
    if "docs.google.com" not in (url_or_id or ""):
        return (url_or_id or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_id)
    if not m:
        raise ValueError(f"Cannot parse spreadsheetId from: {url_or_id}")
    return m.group(1)

def retry_with_backoff(retries=5, initial_delay=2.0):
    def decorator(func):
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except APIError as e:
                    if e.response.status_code in [429, 500, 502, 503]:
                        if i == retries - 1: raise e
                        time.sleep(delay + random.uniform(0, 1))
                        delay *= 2
                    else:
                        raise e
                except Exception as e:
                    if "quota" in str(e).lower() or "rate limit" in str(e).lower():
                        time.sleep(delay)
                        delay *= 2
                    else:
                        raise e
            return func(*args, **kwargs)
        return wrapper
    return decorator

class GSheetsClient:
    """Google Sheets wrapper with Drive Folder support."""

    def __init__(self):
        mode = (settings.GSHEETS_AUTH_MODE or "oauth").lower().strip()
        SCOPES = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive" # Required for folder ops
        ]

        if mode == "service_account":
            from google.oauth2.service_account import Credentials as ServiceCreds
            creds = ServiceCreds.from_service_account_file(
                os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "config/service_account.json"),
                scopes=SCOPES,
            )
        else:
            token_path = os.getenv("GOOGLE_OAUTH_TOKEN_PATH", "config/token.json")
            if not os.path.exists(token_path):
                raise FileNotFoundError(f"❌ token.json not found at {token_path}.")

            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    print("🔄 OAuth Token expired. Refreshing now...")
                    creds.refresh(Request())
                    with open(token_path, 'w') as token:
                        token.write(creds.to_json())
                else:
                    raise Exception("❌ Token is invalid. Re-generate token.json.")
        
        self.creds = creds
        self.gc = gspread.authorize(creds)

    # --- DRIVE API HELPERS ---
    def _get_drive_headers(self):
        """Refreshes token if needed and returns headers."""
        if self.creds.expired:
            self.creds.refresh(Request())
        return {
            "Authorization": f"Bearer {self.creds.token}",
            "Content-Type": "application/json"
        }

    def ensure_folder_exists(self, parent_id: str, folder_name: str) -> str:
        """
        Checks if 'folder_name' exists inside 'parent_id'.
        If yes, returns its ID. If no, creates it and returns new ID.
        """
        headers = self._get_drive_headers()
        
        # 1. Search for existing folder
        query = f"'{parent_id}' in parents and name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        url = f"https://www.googleapis.com/drive/v3/files?q={query}"
        
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            files = resp.json().get('files', [])
            if files:
                print(f"   📂 Found existing folder '{folder_name}' ({files[0]['id']})")
                return files[0]['id']
        
        # 2. Create if not found
        print(f"   Ez Creating new folder '{folder_name}' inside {parent_id}...")
        create_url = "https://www.googleapis.com/drive/v3/files"
        payload = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]
        }
        resp = requests.post(create_url, headers=headers, json=payload)
        if resp.status_code == 200:
            new_id = resp.json().get('id')
            return new_id
        else:
            raise Exception(f"Failed to create folder: {resp.text}")

    def move_file_to_folder(self, file_id: str, folder_id: str):
        """Moves a file into a specific folder (by adding parent, removing old parents)."""
        headers = self._get_drive_headers()
        
        # 1. Get current parents
        get_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=parents"
        resp = requests.get(get_url, headers=headers)
        current_parents = ",".join(resp.json().get('parents', []))
        
        # 2. Move
        move_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?addParents={folder_id}&removeParents={current_parents}"
        resp = requests.patch(move_url, headers=headers)
        
        if resp.status_code == 200:
            print(f"   🚚 Moved file {file_id} -> Folder {folder_id}")
        else:
            print(f"   ⚠️ Failed to move file: {resp.text}")

    # --- EXISTING METHODS ---
    @retry_with_backoff()
    def open(self, spreadsheet_url_or_id: str):
        sid = _extract_sheet_id(spreadsheet_url_or_id)
        return self.gc.open_by_key(sid)

    @retry_with_backoff()
    def worksheet_titles(self, spreadsheet_url_or_id: str) -> list[str]:
        return [ws.title for ws in self.open(spreadsheet_url_or_id).worksheets()]

    @retry_with_backoff()
    def read_tab_values(self, spreadsheet_url_or_id: str, tab_name: str, value_render_option: str = 'FORMATTED_VALUE') -> list[list] | None:
        """Like read_values, but only a missing tab returns None; every other error is raised.

        read_values turns any failure (a 429 included) into [], which is fine for a tab that is
        only shown, not for a tab whose contents decide what must not be written twice.
        """
        sh = self.open(spreadsheet_url_or_id)
        try:
            ws = sh.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            return None
        return ws.get_all_values(value_render_option=value_render_option)

    @retry_with_backoff()
    def read_values(self, spreadsheet_url_or_id: str, tab_name: str, value_render_option: str = 'FORMATTED_VALUE') -> list[list]:
        """Raw cell grid of a tab, as a list of rows. [] when the tab is missing.

        Callers that must locate their header row by content (the S5 raw tab moves it
        whenever a wallet line is added above the grid) read the grid once with this and
        then build the frame with df_from_values, instead of paying for a second fetch.
        """
        sh = self.open(spreadsheet_url_or_id)
        try:
            ws = sh.worksheet(tab_name)
        except Exception:
            print(f"⚠️ Warning: Tab '{tab_name}' not found. Returning empty DataFrame.")
            return []

        return ws.get_all_values(value_render_option=value_render_option)

    @staticmethod
    def df_from_values(values: list[list], header_row: int = 1) -> pd.DataFrame:
        """Build a DataFrame from a raw cell grid, taking `header_row` (1-based) as headers."""
        if not values: return pd.DataFrame()

        header_idx = header_row - 1
        if header_idx >= len(values): return pd.DataFrame()

        header = values[header_idx]
        data = values[header_idx + 1 :]

        df = pd.DataFrame(data, columns=header)
        df = df.replace("", pd.NA).dropna(how="all")
        return df

    def read_as_df(self, spreadsheet_url_or_id: str, tab_name: str, header_row: int = 1, value_render_option: str = 'FORMATTED_VALUE') -> pd.DataFrame:
        values = self.read_values(spreadsheet_url_or_id, tab_name, value_render_option=value_render_option)
        return self.df_from_values(values, header_row)

    @retry_with_backoff()
    def read_as_df_sync(self, spreadsheet_url_or_id: str, tab_name: str) -> pd.DataFrame:
        sh = self.open(spreadsheet_url_or_id)
        try:
            ws = sh.worksheet(tab_name)
        except Exception:
            return pd.DataFrame()
        values = ws.get_all_records()
        return pd.DataFrame(values)

    @retry_with_backoff()
    def create_spreadsheet(self, title: str) -> str:
        try:
            sh = self.gc.create(title)
            return f"https://docs.google.com/spreadsheets/d/{sh.id}"
        except Exception as e:
            print(f"❌ Failed to create spreadsheet '{title}': {e}")
            raise e

    @retry_with_backoff()
    def copy_permissions(self, source_id: str, target_id: str):
        try:
            source_sh = self.open(source_id)
            target_sh = self.open(target_id)
            permissions = source_sh.list_permissions()
            for p in permissions:
                email = p.get('emailAddress')
                role = p.get('role')
                if not email or "iam.gserviceaccount.com" in email: continue
                if role == 'owner': role = 'writer'
                try:
                    target_sh.share(email, perm_type='user', role=role, notify=False)
                except Exception: pass
        except Exception: pass
        
    @retry_with_backoff()
    def update_cell(self, spreadsheet_url_or_id: str, tab_name: str, row: int, col: int, value: str):
        sh = self.open(spreadsheet_url_or_id)
        ws = sh.worksheet(tab_name)
        cell_address = rowcol_to_a1(row, col)
        ws.update(range_name=cell_address, values=[[str(value)]], value_input_option="USER_ENTERED")

    @retry_with_backoff()
    def batch_update_cells(self, spreadsheet_url_or_id: str, tab_name: str, updates: list[dict]):
        if not updates: return
        sh = self.open(spreadsheet_url_or_id)
        ws = sh.worksheet(tab_name)
        batch_payload = []
        for u in updates:
            a1_notation = rowcol_to_a1(u['row'], u['col'])
            batch_payload.append({'range': a1_notation, 'values': [[str(u['val'])]]})
        ws.batch_update(batch_payload, value_input_option="USER_ENTERED")

    @retry_with_backoff()
    def delete_rows(self, spreadsheet_url_or_id: str, tab_name: str, row_indices: list[int]):
        if not row_indices: return
        sh = self.open(spreadsheet_url_or_id)
        try: ws = sh.worksheet(tab_name)
        except Exception: return
        sorted_rows = sorted(list(set(row_indices)), reverse=True)
        for row_num in sorted_rows:
            try: ws.delete_rows(row_num)
            except Exception: pass

    @retry_with_backoff()
    def cleanup_default_sheet(self, spreadsheet_url_or_id: str):
        sh = self.open(spreadsheet_url_or_id)
        worksheets = sh.worksheets()
        if len(worksheets) > 1:
            try:
                ws = sh.worksheet("Sheet1")
                sh.del_worksheet(ws)
            except Exception: pass

    @retry_with_backoff()
    def append_or_create_df(self, spreadsheet_url_or_id: str, tab_name: str, df: pd.DataFrame, 
                            template_tab_name: str | None = None, 
                            template_spreadsheet_id: str | None = None) -> None:
        if df is None or df.empty: return
        target_sh = self.open(spreadsheet_url_or_id)
        target_sid = target_sh.id
        try:
            ws = target_sh.worksheet(tab_name)
            tab_exists = True
        except Exception:
            tab_exists = False

        df_export = df.astype(object).where(pd.notnull(df), None)
        data_values = df_export.values.tolist()

        if not tab_exists:
            created_from_template = False
            if template_tab_name and template_spreadsheet_id:
                try:
                    source_sh = self.open(template_spreadsheet_id)
                    source_ws = source_sh.worksheet(template_tab_name)
                    copy_res = source_ws.copy_to(target_sid)
                    new_sheet_id = copy_res['sheetId']
                    ws = target_sh.get_worksheet_by_id(new_sheet_id)
                    ws.update_title(tab_name)
                    created_from_template = True
                except Exception: pass

            if not created_from_template:
                rows = max(len(df) + 1, 100)
                cols = max(len(df.columns), 26)
                ws = target_sh.add_worksheet(title=tab_name, rows=rows, cols=cols)
                values = [df.columns.tolist()] + data_values
                ws.update("A1", values)
                return

        ws.append_rows(data_values, value_input_option="USER_ENTERED")
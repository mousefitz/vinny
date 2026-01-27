import logging
import base64
import json
import datetime
from zoneinfo import ZoneInfo
from typing import Coroutine, List, Dict, Any
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_client import BaseClient
from . import constants
from cachetools import TTLCache

class FirestoreService:
    def __init__(self, loop: Coroutine, firebase_b64_creds: str, app_id: str):
        self.db = self._initialize_firebase(firebase_b64_creds)
        self.loop = loop
        self.APP_ID = app_id
        self.profile_cache = TTLCache(maxsize=1000, ttl=300)

    def _initialize_firebase(self, firebase_b64_creds: str) -> BaseClient | None:
        if not firebase_b64_creds:
            logging.warning("GOOGLE_APPLICATION_CREDENTIALS_BASE64 not set. Firebase is disabled.")
            return None
        
        if firebase_admin._apps:
            return firestore.client()
        try:
            service_account_info = json.loads(base64.b64decode(firebase_b64_creds).decode('utf-8'))
            cred = credentials.Certificate(service_account_info)
            firebase_admin.initialize_app(cred)
            logging.info("Firebase initialized successfully.")
            return firestore.client()
        except Exception:
            logging.error("Failed to initialize Firebase from Base64 credentials.", exc_info=True)
            return None

    # --- LEDGER & COST TRACKING (FIXED) ---

    async def update_usage_stats(self, date_str: str, increments: dict):
        """
        Updates Daily, Weekly, Monthly, and All-Time stats atomically using Increments.
        increments: {"images": 1, "cost": 0.04, "text_requests": 1, "tokens": 500}
        """
        if not self.db: return
        
        # 1. Calculate Timeframes
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        year, week, day = dt.isocalendar()
        week_str = f"{year}-W{week:02d}"
        month_str = dt.strftime("%Y-%m")
        
        # 2. Define Document Paths
        base_path = constants.get_bot_state_collection_path(self.APP_ID)
        stats_root = self.db.collection(base_path).document("usage_stats")
        
        refs = [
            stats_root.collection("daily_stats").document(date_str),
            stats_root.collection("weekly_stats").document(week_str),
            stats_root.collection("monthly_stats").document(month_str),
            stats_root # Grand Total
        ]

        # 3. Batch Write with Atomic Increments
        # This avoids "contention" because we don't need to read the doc first.
        try:
            batch = self.db.batch()
            
            update_data = {
                "images": firestore.Increment(increments.get("images", 0)),
                "text_requests": firestore.Increment(increments.get("text_requests", 0)),
                "tokens": firestore.Increment(increments.get("tokens", 0)),
                "estimated_cost": firestore.Increment(increments.get("cost", 0.0))
            }
            
            for ref in refs:
                batch.set(ref, update_data, merge=True)

            await self.loop.run_in_executor(None, batch.commit)
            logging.info(f"💰 Ledger updated for {date_str} (Daily/Weekly/Monthly/Total)")
            
        except Exception:
            logging.error("Failed to update usage ledger.", exc_info=True)
    
    async def add_doc(self, collection_path: str, data: dict):
        if not self.db: return None
        try:
            collection_ref = self.db.collection(collection_path)
            _, doc_ref = await self.loop.run_in_executor(None, lambda: collection_ref.add(data))
            return {"id": doc_ref.id}
        except Exception:
            logging.error(f"Failed to add document to '{collection_path}'", exc_info=True)
            return None

    async def get_docs(self, collection_path: str) -> List[Dict[str, Any]]:
        if not self.db: return []
        try:
            collection_ref = self.db.collection(collection_path)
            docs_snapshot = await self.loop.run_in_executor(None, collection_ref.stream)
            return [doc.to_dict() for doc in docs_snapshot]
        except Exception:
            logging.error(f"Failed to get documents from '{collection_path}'", exc_info=True)
            return []

    async def delete_docs(self, collection_path: str):
        if not self.db: return False
        try:
            collection_ref = self.db.collection(collection_path)
            docs_snapshot = await self.loop.run_in_executor(None, collection_ref.stream)
            for doc in docs_snapshot:
                await self.loop.run_in_executor(None, doc.reference.delete)
            return True
        except Exception:
            logging.error(f"Failed to delete documents from '{collection_path}'", exc_info=True)
            return False

    async def save_user_profile_fact(self, user_id: str, guild_id: str | None, key: str, value: str):
        if not self.db: return False
        cache_key = f"{user_id}_{guild_id}"
        if cache_key in self.profile_cache:
            del self.profile_cache[cache_key]
        collection_path = constants.get_user_profile_collection_path(self.APP_ID, guild_id)
        doc_ref = self.db.collection(collection_path).document(user_id)
        try:
            await self.loop.run_in_executor(None, lambda: doc_ref.set({key: value}, merge=True))
            return True
        except Exception:
            logging.error(f"Failed to save fact for user {user_id}", exc_info=True)
            return False

    async def get_user_profile(self, user_id: str, guild_id: str | None) -> dict:
        if not self.db: return {}
        cache_key = f"{user_id}_{guild_id}"
        if cache_key in self.profile_cache:
            return self.profile_cache[cache_key]
        global_path = constants.get_global_user_profiles_path(self.APP_ID)
        server_path = constants.get_user_profile_collection_path(self.APP_ID, guild_id) if guild_id else None
        
        global_doc_ref = self.db.collection(global_path).document(user_id)
        global_doc = await self.loop.run_in_executor(None, global_doc_ref.get)
        global_profile = global_doc.to_dict() if global_doc.exists else {}

        server_profile = {}
        if server_path:
            server_doc_ref = self.db.collection(server_path).document(user_id)
            server_doc = await self.loop.run_in_executor(None, server_doc_ref.get)
            server_profile = server_doc.to_dict() if server_doc.exists else {}
            
        full_profile = global_profile | server_profile
        self.profile_cache[cache_key] = full_profile
        return full_profile

    async def delete_user_profile(self, user_id: str, guild_id: str):
        if not self.db: return False
        try:
            path = constants.get_user_profile_collection_path(self.APP_ID, guild_id)
            await self.loop.run_in_executor(None, self.db.collection(path).document(user_id).delete)
            return True
        except Exception:
            logging.error(f"Failed to delete profile for user '{user_id}' in guild '{guild_id}'", exc_info=True)
            return False

    async def delete_user_profile_fact(self, user_id: str, guild_id: str | None, fact_key: str):
        if not self.db or not fact_key: return False
        path = constants.get_user_profile_collection_path(self.APP_ID, guild_id)
        profile_ref = self.db.collection(path).document(user_id)
        try:
            await self.loop.run_in_executor(None, lambda: profile_ref.update({fact_key: firestore.DELETE_FIELD}))
            return True
        except Exception:
            logging.error(f"Failed to delete fact '{fact_key}' for user '{user_id}'", exc_info=True)
            return False
    
    async def get_all_user_ids_in_guild(self, guild_id: str):
        if not self.db: return []
        try:
            users_ref = self.db.collection('guilds').document(str(guild_id)).collection('users')
            docs = users_ref.stream()
            return [doc.id for doc in docs]
        except Exception as e:
            logging.error(f"Failed to fetch all users for guild {guild_id}: {e}")
            return []
        
    async def update_relationship_score(self, user_id: str, guild_id: str, sentiment_score: int):
        """Updates a user's relationship score and returns the new total."""
        if not self.db: return 0
        
        # --- FIX: Invalidate the Cache so !vibe sees the change immediately ---
        cache_key = f"{user_id}_{guild_id}"
        if cache_key in self.profile_cache:
            del self.profile_cache[cache_key]
        # ----------------------------------------------------------------------
        
        path = constants.get_user_profile_collection_path(self.APP_ID, guild_id)
        doc_ref = self.db.collection(path).document(user_id)

        try:
            @firestore.transactional
            def update_in_transaction(transaction, doc_ref_to_update):
                snapshot = doc_ref_to_update.get(transaction=transaction)
                current_score = snapshot.to_dict().get("relationship_score", 0) if snapshot.exists else 0
                
                new_score = current_score + sentiment_score
                new_score = max(-100, min(100, new_score)) # Optional: Clamp it nicely
                new_score *= 0.995 # Decay slightly towards neutral
                
                transaction.set(doc_ref_to_update, {"relationship_score": new_score}, merge=True)
                return new_score

            new_score = await self.loop.run_in_executor(None, update_in_transaction, self.db.transaction(), doc_ref)
            return new_score
        except Exception:
            logging.error(f"Failed to update relationship score for user '{user_id}'", exc_info=True)
            return 0
        
    async def save_user_nickname(self, user_id: str, nickname: str):
        if not self.db: return False
        try:
            path = constants.get_user_details_path(self.APP_ID, user_id)
            profile_ref = self.db.collection(path).document('details')
            await self.loop.run_in_executor(None, lambda: profile_ref.set({'nickname': nickname}, merge=True))
            return True
        except Exception:
            logging.error(f"Failed to save nickname for user '{user_id}'", exc_info=True)
            return False

    async def get_user_nickname(self, user_id: str) -> str | None:
        if not self.db: return None
        try:
            path = constants.get_user_details_path(self.APP_ID, user_id)
            doc = await self.loop.run_in_executor(None, self.db.collection(path).document('details').get)
            return doc.to_dict().get('nickname') if doc.exists else None
        except Exception:
            logging.error(f"Failed to get nickname for user '{user_id}'", exc_info=True)
            return None

    async def save_memory(self, guild_id: str, summary_data: dict):
        if not self.db: return
        path = constants.get_summaries_collection_path(self.APP_ID, guild_id)
        doc_data = {
            "timestamp": datetime.datetime.now(datetime.UTC),
            "summary": summary_data.get("summary", ""),
            "keywords": summary_data.get("keywords", [])
        }
        await self.add_doc(path, doc_data)

    async def retrieve_server_summaries(self, guild_id: str):
        if not self.db: return []
        path = constants.get_summaries_collection_path(self.APP_ID, guild_id)
        return await self.get_docs(path)
    
    async def retrieve_relevant_memories(self, guild_id: str, query_keywords: list, limit: int = 2):
        if not self.db or not query_keywords:
            return []
        path = constants.get_summaries_collection_path(self.APP_ID, guild_id)
        try:
            collection_ref = self.db.collection(path)
            docs_query = collection_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(48)
            docs_snapshot = await self.loop.run_in_executor(None, docs_query.stream)
            all_docs = [doc.to_dict() for doc in docs_snapshot]
            relevant_docs = []
            for doc in all_docs:
                searchable_text = doc.get("summary", "").lower()
                searchable_keywords = [k.lower() for k in doc.get("keywords", [])]
                if any(qk.lower() in searchable_text or qk.lower() in searchable_keywords for qk in query_keywords):
                    relevant_docs.append(doc)
            return relevant_docs[:limit]
        except Exception:
            logging.error(f"Failed to retrieve relevant memories for guild '{guild_id}'", exc_info=True)
            return []

    async def save_proposal(self, proposer_id: str, recipient_id: str):
        if not self.db: return False
        try:
            path = constants.get_proposals_collection_path(self.APP_ID)
            doc_data = {
                "proposer_id": proposer_id,
                "recipient_id": recipient_id,
                "timestamp": datetime.datetime.now(datetime.UTC)
            }
            await self.loop.run_in_executor(None, self.db.collection(path).document(f"{proposer_id}_to_{recipient_id}").set, doc_data)
            return True
        except Exception:
            logging.error(f"Failed to save proposal from '{proposer_id}' to '{recipient_id}'", exc_info=True)
            return False

    async def check_proposal(self, proposer_id: str, recipient_id: str):
        if not self.db: return None
        try:
            path = constants.get_proposals_collection_path(self.APP_ID)
            doc = await self.loop.run_in_executor(None, self.db.collection(path).document(f"{proposer_id}_to_{recipient_id}").get)
            if doc.exists:
                proposal_time = doc.to_dict().get("timestamp")
                if isinstance(proposal_time, datetime.datetime) and proposal_time.tzinfo is None:
                    proposal_time = proposal_time.replace(tzinfo=datetime.UTC)
                if (datetime.datetime.now(datetime.UTC) - proposal_time) < datetime.timedelta(minutes=5):
                    return doc.to_dict()
        except Exception:
            logging.error(f"Failed to check proposal from '{proposer_id}' to '{recipient_id}'", exc_info=True)
        return None

    async def finalize_marriage(self, user1_id: str, user2_id: str):
        if not self.db: return False
        try:
            date = datetime.datetime.now(datetime.UTC).astimezone(ZoneInfo("America/New_York")).strftime("%B %d, %Y")
            await self.save_user_profile_fact(user1_id, None, "married_to", user2_id)
            await self.save_user_profile_fact(user1_id, None, "marriage_date", date)
            await self.save_user_profile_fact(user2_id, None, "married_to", user1_id)
            await self.save_user_profile_fact(user2_id, None, "marriage_date", date)
            proposal_path = constants.get_proposals_collection_path(self.APP_ID)
            await self.loop.run_in_executor(None, self.db.collection(proposal_path).document(f"{user1_id}_to_{user2_id}").delete)
            return True
        except Exception:
            logging.error(f"Failed to finalize marriage between '{user1_id}' and '{user2_id}'", exc_info=True)
            return False

    async def process_divorce(self, user1_id: str, user2_id: str):
        if not self.db: return False
        try:
            global_path = constants.get_global_user_profiles_path(self.APP_ID)
            update_data = {"married_to": firestore.DELETE_FIELD, "marriage_date": firestore.DELETE_FIELD}
            await self.loop.run_in_executor(None, self.db.collection(global_path).document(user1_id).update, update_data)
            await self.loop.run_in_executor(None, self.db.collection(global_path).document(user2_id).update, update_data)
            return True
        except Exception:
            logging.error(f"Failed to process divorce for '{user1_id}'", exc_info=True)
            return False
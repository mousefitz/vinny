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
        # NEW: Cache up to 1000 profiles for 5 minutes (300 seconds)
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

    # --- Generic Firestore Operations ---

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

    # --- User Profile Management ---

    async def save_user_profile_fact(self, user_id: str, guild_id: str | None, key: str, value: str):
        if not self.db: return False
        
        # NEW: Invalidate cache because data is changing
        cache_key = f"{user_id}_{guild_id}"
        if cache_key in self.profile_cache:
            del self.profile_cache[cache_key]

        # Existing Save Logic
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
        
        # NEW: Check cache first
        cache_key = f"{user_id}_{guild_id}"
        if cache_key in self.profile_cache:
            return self.profile_cache[cache_key]

        # Existing Fetch Logic
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
        
        # NEW: Save to cache
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
        """Retrieves a list of all user IDs that have a profile in this guild."""
        if not self.db: return []
        
        try:
            
            users_ref = self.db.collection('guilds').document(str(guild_id)).collection('users')
            
            
            docs = users_ref.stream()
            
            return [doc.id for doc in docs]
        except Exception as e:
            logging.error(f"Failed to fetch all users for guild {guild_id}: {e}")
            return []
        
    # --- Relationship Score Management ---

    async def update_relationship_score(self, user_id: str, guild_id: str, sentiment_score: int):
        """Updates a user's relationship score and returns the new total."""
        if not self.db: return 0
        
        path = constants.get_user_profile_collection_path(self.APP_ID, guild_id)
        doc_ref = self.db.collection(path).document(user_id)

        try:
            @firestore.transactional
            def update_in_transaction(transaction, doc_ref_to_update):
                snapshot = doc_ref_to_update.get(transaction=transaction)
                current_score = snapshot.to_dict().get("relationship_score", 0) if snapshot.exists else 0
                
                new_score = current_score + sentiment_score
                new_score *= 0.995 
                
                transaction.set(doc_ref_to_update, {"relationship_score": new_score}, merge=True)
                return new_score

            new_score = await self.loop.run_in_executor(None, update_in_transaction, self.db.transaction(), doc_ref)
            return new_score
        except Exception:
            logging.error(f"Failed to update relationship score for user '{user_id}'", exc_info=True)
            return 0
        
    # --- Nickname Management ---

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

    # --- Memory Summaries ---

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
        """
        Retrieves the most relevant memory summaries based on keywords.
        """
        if not self.db or not query_keywords:
            return []

        path = constants.get_summaries_collection_path(self.APP_ID, guild_id)
        
        try:
            collection_ref = self.db.collection(path)
            
            docs_query = collection_ref.order_by(
                "timestamp", direction=firestore.Query.DESCENDING
            ).limit(48)
            
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
        
    async def retrieve_general_memories(self, guild_id: str, query_keywords: list, limit: int = 2):
        if not self.db: return []
        path = constants.get_summaries_collection_path(self.APP_ID, guild_id)
        docs = await self.get_docs(path)
        relevant = [doc for doc in docs if any(qk.lower() in (dk.lower() for dk in doc.get("keywords", [])) or qk.lower() in doc.get("summary", "").lower() for qk in query_keywords)]
        return sorted(relevant, key=lambda x: x.get('timestamp', ''), reverse=True)[:limit]

    # --- Marriage & Proposals ---

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
            
    # --- Rate Limiter Documents ---
    
    async def get_rate_limit_doc(self):
        if not self.db: return None
        path = constants.get_bot_state_collection_path(self.APP_ID)
        doc_ref = self.db.collection(path).document('rate_limit')
        doc = await self.loop.run_in_executor(None, doc_ref.get)
        return doc.to_dict() if doc.exists else None

    async def set_rate_limit_doc(self, data: dict):
        if not self.db: return
        path = constants.get_bot_state_collection_path(self.APP_ID)
        doc_ref = self.db.collection(path).document('rate_limit')
        await self.loop.run_in_executor(None, lambda: doc_ref.set(data))

    async def update_rate_limit_doc(self, data: dict):
        if not self.db: return
        path = constants.get_bot_state_collection_path(self.APP_ID)
        doc_ref = self.db.collection(path).document('rate_limit')
        await self.loop.run_in_executor(None, doc_ref.update, data)
// Copy this file to config.js and fill in real values during deployment.
// config.js is git-ignored. The deploy script (deploy/droplet/painminer-deploy.sh)
// generates config.js from .env automatically.
//
// Threat model:
// - The anon key is public-by-design; RLS on pain_posts blocks anon from writing.
// - The only write path is the set_pain_post_status RPC, which requires
//   rpcSecret to match the row in pain_miner_secrets. Without that secret,
//   even a caller with the anon key gets 'unauthorized' on every RPC call.
// - rpcSecret therefore belongs ONLY to authenticated browsers. config.js sits
//   inside /var/www/painminer behind nginx basic auth (chmod 640) — anyone who
//   can read it has already passed the human gate.
window.PAINMINER_CONFIG = {
  supabaseUrl: "https://YOUR-PROJECT.supabase.co",
  supabaseAnonKey: "eyJhbGc...REPLACE_ME",
  rpcSecret: "REPLACE_ME_with_PAINMINER_RPC_SECRET_from_.env",
};

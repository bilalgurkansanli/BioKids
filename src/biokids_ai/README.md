# `biokids-ai`: Sor Bana

The child's local assistant (D-165), and English with Sio (D-226). An optional
package, off until a parent turns it on for a child, and nothing else in BioKids
imports it. It runs as `biokids-ai.service`: the `biokids-ai` user, no network
(`PrivateNetwork=yes`), answering the daemon on `/run/biokids-ai/assistant.sock`.
The models are downloaded once, from the panel, by `biokids-ai-setup.service`,
each file pinned by SHA-256 (`models.py`); none is ever shipped in a package.

## A question, from the screen to the answer

1. **The word list** (`safety.py`, `data/lexicon.toml`): Turkish and English, folded
   for spelling tricks. A hit is refused at once, with a sentence people wrote.
   A question about the helper itself or BioKids - who made it, its name, what
   BioKids is - is then answered with sentences people wrote (`identity.py`), in
   the question's language, without the classifier or the model, which does not
   know; each sentence still passes the word list on its way out.
2. **The classifier** (`prompts.CLASSIFY_SYSTEM`, `llm.py`): the same model, asked
   only whether a kind teacher would answer this for a child of this age, as JSON
   with a fixed set of categories and worked examples (few-shot). Unsure means no.
3. **What it can be given as fact** (retrieval, D-178 and D-248): the opening
   paragraphs of the encyclopedia's articles on the subject (`knowledge.py`,
   `kiwix-serve` over Kiwix's Wikipedia packs, unless the family turned that off,
   D-247), and the worked answer of a sum in the question (`arithmetic.py`):
   a small model guesses at arithmetic.
4. **The answer** (`prompts.ANSWER_SYSTEM`): instructions that name the helper, the
   child and their age, the length for the age band, one worked example, and the
   facts from step 3. Streamed a sentence at a time (`sentences.py`), and every
   sentence passes the word list again before the child sees it (D-216).
5. **The language**: the answer is in the language of the question, Turkish or
   English (`biokids_core.language`, D-244); speech is heard with Whisper
   (`stt.py`), which guesses the language and is asked again in the child's own
   when it hears neither.

No fine-tuning: the model is Google's Gemma as published, steered by the
instructions, the examples and the facts it is given (D-244). `dev/assistant_eval.py`
asks a real model a test set and scores the answers (D-248).

## The servers

`runtime.ManagedServer` starts `llama-server`, `whisper-server` and `kiwix-serve`
when they are first needed and stops them after ten idle minutes; their last lines
of error output are kept to say why one stopped.

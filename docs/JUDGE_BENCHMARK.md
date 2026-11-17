# Judge v2 benchmark (17 Nov 2026, before the API key)

How it was run: the 5 cases below went through the real v2 serializers (`judge/compact.py`, 8 frames from
`keyframes_lowres`). Two Claude Code subagents (Haiku-class and Sonnet-class) acted as the API: each read
`prompts/judge_v2.md` as its system prompt and every case's labelled frames + compact JSON, and answered in
the output schema. These are **proxies** for `claude-haiku-5-5` / `claude-sonnet-5-5`, not the API models.
Token counts are **estimates** (text: chars/3.5; images: w*h/750). Run
`scripts/judge_cost_benchmark.py BENCH --exact` with `ANTHROPIC_API_KEY` set for counts from `count_tokens`.

## Cases
| case | source | what it tests |
|---|---|---|
| R1_kaicenat_real | real VOD clip, real whisper (empty), real chat | big chat spike, nothing on screen |
| R2_kenetgr_kick_real | real Kick live clip, real Spanish transcript + chat | normal GTA RP moment, es -> it copy |
| A_silent_visual | real officermesser GTA frames, no transcript | judging from frames only |
| B_italian_hype | real matteu frames, synthetic Italian transcript + 48-msg chat burst | Italian copy |
| C_music_spoiler | real illojuan frames, synthetic transcript: invented lyrics + [music] + invented story spoiler | flags + reject |

## Tokens per clip (estimated)
| part | tokens |
|---|---|
| static system prompt (cached from clip 2) | ~2,094 |
| 8 frames at 480x270 (~173 each) + labels | 1,432 |
| compact JSON payload | 57-284 (avg ~147) |
| output (haiku / sonnet) | ~110 / ~106 |

Before (v1): 6 frames at 853x480 (~546 each = 3,276), prompt under the 512-token cache minimum so never
cached, per-word timestamped transcript, 60 raw chat lines, verbose output with 3 titles and thinking on.

## Cost (USD, prices from judge.prices; Haiku cache read assumed 0.1x input)
| setup | 1st clip (miss) | next clips (hit) | batch, hit | 50 clips | 1,500/mo | clips on $600 |
|---|---|---|---|---|---|---|
| v1 before (Sonnet, est.) | $0.01494 | $0.01494 | $0.00747 | $0.747 | $22.41 | 40,158 |
| Haiku 5.5 single call | $0.00047 | $0.00023 | $0.00012 | $0.012 | $0.35 | 2,568,273 |
| Sonnet 5.5 single call | $0.00945 | $0.00442 | $0.00221 | $0.226 | $6.63 | 135,654 |
| Haiku screen -> Sonnet judge (40% pass) | $0.00409 | $0.00183 | $0.00092 | $0.094 | $2.75 | 327,007 |

- Cache hits need traffic within 5 minutes of the previous call (default TTL); a quiet night pays the
  miss price more often (still under 1 cent per clip on Sonnet).
- Batch (50% off) returns in minutes to hours: VOD lane only, never the live lane.
- The two-stage screen is text-only, so it cannot see silent visual fails (case A) and rejects them.
- Decision: `judge.mode: single` on `claude-sonnet-5-5`. At ~$0.0044/clip, quality matters more than the
  ~$6/month difference to Haiku.

## Outputs
Sonnet-class:
| case | ok | sc | hk | tt | fl |
|---|---|---|---|---|---|
| A | no | 3 | WAIT FOR IT... | He bailed out of the car for THIS... 😭 | - |
| B | yes | 6 | MA COSA HA FATTO?! 💀 | Giurava di non averlo toccato... 💀 | - |
| C | no | 3 | SPOILER 💀 | Ha detto la fine della storia in live... | sp, mu |
| R1 | no | 3 | WAKE UP 😳 | 21K people watching him sleep... | - |
| R2 | yes | 7 | ERA QUASI IN TESTA 😳 | Gli ha sparato a un soffio dalla testa... | - |

Haiku-class:
| case | ok | sc | hk | tt | fl |
|---|---|---|---|---|---|
| A | no | 4 | WAIT FOR IT 💀 | Officermesser thought he was safe... 💀 | - |
| B | yes | 7 | NON DOVEVA ANDARE COSÌ 😳 | Matteu doveva solo passare di lì... 💀 | - |
| C | no | 2 | NON DIRLO IN LIVE 💀 | Illojuan non doveva dirlo in live... 😭 | sp, mu |
| R1 | no | 4 | CHAT WON'T STOP 💀 | Chat refused to let Kaicenat sleep... 💀 | - |
| R2 | yes | 6 | TROPPO VICINO, GUARDATE 😳 | Kenetgr pensava di essere al sicuro... 😭 | - |

Every answer was valid JSON within the caps. Both caught music + spoiler in C and rejected the two dead
moments (R1, A). Sonnet's copy is more specific to what happens; Haiku reuses the template phrases
("thought he was safe", "nobody saw it coming") and wrote a payoff for A that the frames don't show.
Sonnet's R2 title gives away the near-miss, which the tease rule forbids; watch for that once real calls run.

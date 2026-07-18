# InnerView Reasoning Audit

**Fecha de auditoria:** 2026-07-14
**Alcance:** arquitectura, razonamiento contable, contratos de salida, modelos, UX, observabilidad, rendimiento y seguridad.
**Metodo:** inspeccion estatica del repositorio, lectura de configuracion efectiva no secreta, muestreo de artefactos runtime, pruebas locales aisladas y observacion del frontend en navegador.
**Restriccion respetada:** esta auditoria no implementa correcciones, no cambia reglas, no agrega vendors/GLs/casos y no modifica documentos fuente.

## Executive Summary

InnerView ya es mucho mas que un OCR: posee parsers deterministas, ingestion multiformato, OCR, Vision, extraccion AI, reglas canonicas, normalizacion, trazas, revision, aprendizaje de correcciones y exportacion ResMan. Sin embargo, **todavia no opera como un unico accounting-grade document reasoning engine**. Opera como una cadena de subsistemas parcialmente superpuestos que pueden extraer, reparar, reclasificar y sobrescribir el mismo campo con contratos distintos.

La conclusion principal es directa:

> El problema dominante no es solamente el modelo actual. Es la falta de una autoridad central, tipada y verificable para semantica, GL, readiness y exportacion. Un modelo mas fuerte mejoraria casos ambiguos, pero no corregiria por si solo los contratos fragmentados ni impediria outputs invalidos.

### Diagnostico ejecutivo

| Dimension | Estado | Diagnostico |
|---|---|---|
| Extraction | Funcional, desigual | Buena en plantillas conocidas; variable en documentos desconocidos, truncados o multipagina. |
| Determinismo | Amplio pero fragmentado | Hay muchos procesadores especificos; la seleccion y el fallback no auditan completitud semantica de forma uniforme. |
| Semantica | Parcial | Existe un clasificador util para service invoices, pero no una ontologia universal aplicada a todos los documentos. |
| GL reasoning | Parcialmente explicable | Distingue materiales/servicio en varios casos, pero usa prioridades y keywords hardcoded y compite con defaults, AI, reglas canonicas y correcciones aprendidas. |
| Required fields | Definidos, no universalmente gobernados | La capa canonica los bloquea, pero readiness de UI y rutas legacy de exportacion no comparten una unica decision. |
| Confidence | No calibrada | Se acepta confidence del proveedor, se mezcla con reparaciones y puede seguir alta aunque el documento requiera review. |
| Accounting readiness | Fragmentada | No existe como objeto unico con estado, blockers, evidencia y version de reglas. |
| Model routing | Incompleto | No existe una ruta separada hacia un strong accounting reasoner; extraccion y sugerencia GL comparten prompt/modelo. |
| Aprendizaje | Correcciones persistentes, no self-training gobernado | No hay lifecycle de propuesta, aprobacion, backtest, versionado, conflicto y rollback. |
| Observabilidad | Parcial | Hay performance, review, trace y provenance, pero no un decision ledger integral por invoice/linea. |
| Performance | Aceptable en UI simple, costosa en pipeline | `batch.total` mediana observada 25.7 s y P95 74.5 s; vendor detection y llamadas externas dominan. |
| Seguridad | Adecuada solo para localhost controlado | No hay autenticacion/autorizacion; documentos y operaciones quedan expuestos si el backend se publica. |
| Testabilidad | Muchos smoke tests, benchmark insuficiente | El fixture canonico `tk_elevator` falla actualmente; Billing V2 smoke no arranca por dependencia faltante. |

### Veredicto arquitectonico

La arquitectura puede evolucionar al producto deseado, pero **no mediante mas parches por vendor o mas reglas dispersas**. Requiere una refactorizacion focalizada alrededor de cuatro contratos centrales:

1. `DocumentFacts`: hechos extraidos con fuente, bbox/text span y confidence de extraccion.
2. `SemanticClassification`: familia documental y semantica por linea, independiente del GL.
3. `AccountingDecision`: candidatos GL, compatibilidad, evidencia, alternativa descartada y confidence calibrada.
4. `AccountingReadiness`: blockers exhaustivos y una sola autorizacion de exportacion.

Los parsers, OCR, referencias, UI y reglas actuales son reutilizables. Lo que falta es un orquestador/decision ledger que impida que cada capa defina su propia verdad.

## Audit Scope and Evidence

### Componentes inspeccionados

- Backend FastAPI: `webapp/backend/main.py`, routers en `webapp/backend/api/`.
- Orquestacion: `batch_processor.py`, `processing_queue.py`, `processing.py`.
- Ingestion/OCR/Vision: `document_ingestion.py`, `uploads.py`, `ai_vision.py`.
- AI: `ai_provider.py`, `ai_invoice_processor.py`, `ai_mapping_review.py`.
- Razonamiento/reglas: `canonical_rules.py`, `service_invoice_gl_reasoning.py`, `description_builder.py`, `learned_corrections.py`.
- Contratos/exportacion: `row_normalizer.py`, `output_contract_validator.py`, `batch_processor.export_batch()`.
- Frontend: `App.tsx`, `TemplateWorkspace.tsx`, `ResManTemplatePreview.tsx`, `DocumentPreviewPanel.tsx`, `BillingV2.tsx` y estilos.
- Configuracion: `.env` solo para valores no secretos, `config/canonical_rules.yaml`, catalogos y vendor rules.
- Runtime: caches, trazas y `performance.jsonl` de batches recientes, sin alterar datos.
- Browser: Billing V2 en `http://localhost:5174/`, viewport 1280x720.

### Validaciones ejecutadas

| Validacion | Resultado |
|---|---|
| `python scripts/verify_backend_routes.py` | PASS |
| `python scripts/smoke_canonical_rules_engine.py` | PASS |
| `python scripts/smoke_required_fields_contract.py` | PASS |
| `python scripts/smoke_canonical_invoice_fixtures.py` | FAIL: `tk_elevator` line items |
| Cuatro funciones `test_*` de `test_service_invoice_gl_reasoning.py`, invocadas directamente | 4/4 PASS |
| `python -m unittest webapp.backend.tests.test_service_invoice_gl_reasoning` | 0 tests descubiertos; no valida nada |
| `python scripts/smoke_billing_v2_contract.py` | No ejecutable: falta `httpx` |
| `npm.cmd run build` | PASS; TypeScript y Vite compilan |

El fallo `tk_elevator` es concreto: la segunda linea esperada `Trip Charge` se convierte en la descripcion combinada del invoice (`Governor Switch...; Trip Charge`). Esto demuestra una regresion vigente entre semantica de linea y composicion de descripcion.

## Current Pipeline Map

```text
POST /api/batches/{batch_id}/upload
  -> uploads.upload_file_endpoint
    -> persistencia en webapp_data/batches/<id>/input
    -> metadata barata / page count

POST /api/batches/{batch_id}/process
  -> processing API / processing_queue
    -> batch_processor.process_batch
      -> batch_store.list_input_files
      -> vendor_detection.detect_vendor_fast / detect_all
      -> document_ingestion.ingest_document (cuando hace falta)
        -> PDF text layer / OCR / image preprocessing
        -> calidad, source_type, needs_vision
      -> route por vendor/documento
        -> deterministic utility/vendor processor
        -> ai_invoice_processor.process_ai_vendor_files
          -> segmentation multipagina/multi-invoice
          -> AI text o AI vision
          -> validate_ai_extraction
          -> reference matching vendor/property/location/GL
          -> canonical_rules.canonicalize_normalized_invoice
            -> GL rules y service_invoice_gl_reasoning
            -> canonical descriptions
            -> required-field validation
            -> reconciliation/review flags
          -> invoice_to_rows / normalize preview rows
      -> cache _webapp_result.json + revision/review/performance/trace

GET preview/manual-review
  -> relee cache completo
  -> renormaliza filas/review

POST export
  -> batch_processor.export_batch
    -> edited/cached rows: normalize -> Dropbox links -> required validation
       -> copy Output/Template.xlsx to batch export -> write rows
    -> legacy fallback: copy latest processor workbook when no usable preview
```

### Etapas, contratos y fallas

| Etapa | Archivo / funcion | Input | Output | Fallas posibles | Logs/tests |
|---|---|---|---|---|---|
| Upload | `api/uploads.py::upload_file_endpoint` | multipart file | archivo + metadata | sin limite explicito de bytes; extension, no MIME real; duplicados renombrados | metadata; smoke ingestion |
| File detection | `document_ingestion.py`, `_source_type_from_suffix` | path/extension | source type/support | tipo real distinto a extension; PDFs corruptos | ingestion preview; smoke ingestion |
| Vendor routing | `vendor_detection.py`, `batch_processor.process_batch` | filename/text inicial | vendor key/confidence | keywords ambiguas; OCR costoso; registry hardcoded | performance `vendor.detect_all`; processor smokes |
| OCR/text | `document_ingestion.ingest_document` | PDF/image/doc | pages/text/quality | scans pobres, multipagina truncada, retries costosos | warnings/cache/perf; ingestion tests |
| Vision decision | `_should_use_vision_for_file/candidate` | source type/quality/config | boolean/model | solo primeras paginas por default; calidad mal estimada | extraction mode/warnings/vision trace |
| AI text | `ai_provider.extract_invoice_structured` | texto + refs + schema | JSON | truncacion 45k, JSON no estricto-schema, 2 intentos | cache, provenance, perf |
| AI vision | `extract_invoice_vision_structured` | hasta 2 page images + refs | JSON + candidates/bboxes | pagina relevante fuera del limite; 3 HTTP attempts + repair | trace JSON, cache, perf |
| Normalization | `validate_ai_extraction` | provider JSON | normalized invoice/issues | reparaciones pueden esconder incertidumbre; confidence proveedor | validation flags/provenance; smokes |
| Property/location | AI processor + references/utilities | candidates/address/history | abbreviation/location | referencias parciales; billing vs service address; history overreach | mapping provenance/review |
| Semantic classification | `service_invoice_gl_reasoning.classify_line_item_semantics` | line/vendor/invoice | trade/work mode/assets | cobertura parcial/hardcoded; se omite en categorias excluidas | 4 pruebas directas |
| GL | canonical rules + GL reasoner + mappings | semantics/defaults/history/AI | GL/candidates/explanation | multiples autoridades; prioridad no global; alternativas pobres | row meta + popup; no benchmark amplio |
| Descriptions | `description_builder` + canonical formats | invoice/line context | invoice/line descriptions | mezcla de resumen global y linea; fixture fallando | description smokes/fixtures |
| Validation | canonical required + output validator | invoice/rows | blockers/review | contratos distintos por capa/ruta | required-field smoke |
| Readiness | backend summary + frontend task builders | flags/confidence/fields | Ready/Needs Review | distintas definiciones y dismiss local | UI e2e parcial |
| Export | `batch_processor.export_batch` | edited/cached/legacy workbook | XLSX | legacy copy evita contrato central; UI no bloquea preventivamente | export smokes parciales |

## Required Field Contract Audit

El contrato canonico declara obligatorios: Invoice Number, Bill/Credit, Invoice Date, Accounting Date, Vendor, Invoice Description, Line Item Number, Property Abbreviation, GL Account, Line Item Description, Amount, Expense Type, Is Replacement Reserve, Due Date y Document Url. Location es opcional.

| Field | Required? | Backend validation | UI validation | Can export blank? | Current risk |
|---|---:|---|---|---|---|
| Vendor | Si | AI validation + canonical + row validator | Parcial segun vista | Modern path: no; legacy copy: no garantia | High |
| Invoice Number/fallback | Si | genera fallback estable y marca review; canonical/row validator | Single/Billing V2 lo revisan | Modern: no; legacy: no garantia | Medium |
| Invoice Date | Si | AI/date validation + canonical | Legacy Single deriva de forma incompleta; Billing V2 por required cols | Modern: no; legacy: no garantia | High |
| Accounting Date | Si | actualmente derivada de invoice date en canonical | Required columns en grid; no regla universal de periodo contable | Modern: no; legacy: no garantia | High |
| Bill/Credit | Si | default `Bill` si falta | normalmente presente | Modern: no; default puede ser semanticamente incorrecto | Medium |
| Property | Si | reference resolution + canonical blocker | Single/Billing V2 la reconocen | Modern: no; legacy: no garantia | Critical |
| GL Account | Si | candidate validation + canonical blocker + output validator | Billing V2 filter lo ve; boton Export no se deshabilita por blocker | Modern: no; legacy workbook copy: posible | Critical |
| Amount | Si | numeric/reconciliation + canonical/row validator | no readiness uniforme en legacy Single | Modern blank: no; monto incorrecto aun puede pasar si reconcilia | Critical |
| Invoice Description | Si | canonical builder + required validation | legacy review derivado no la cubre exhaustivamente | Modern blank: no; descripcion mala puede pasar | High |
| Line Item Description | Si | canonical builder + per-line validation | no blocker exhaustivo en todas las vistas | Modern blank: no; semantica incorrecta puede pasar | High |
| Due Date | Si actual | canonical required | omitida por `deriveRequiredReviewFlags` legacy | Modern blank: no; status visual puede ser engañoso | High |
| Document Url | Si actual | modern export intenta Dropbox y valida | Billing V2 muestra link count; no bloqueo preventivo universal | Modern exige valor; local URL puede existir antes de export | High |
| Total reconciliation | Regla critica, no columna | AI validation y canonical summary | visible indirectamente como review | no se garantiza en legacy workbook copy | Critical |

### Respuestas directas sobre GL

- **Can a line item reach Ready with blank GL Account?** En el flujo canonico/Billing V2 correctamente actualizado, no deberia. Globalmente no existe una garantia unica: readiness legacy y estados stale se calculan con otra logica.
- **Can a line item be exported with blank GL Account?** Las rutas modernas de rows lo rechazan. La ruta que copia un workbook legacy no ejecuta el mismo contrato, por lo que no puede afirmarse una garantia global.
- **Does missing GL trigger fallback reasoning?** Si, en varios caminos: learned mapping, AI candidate, canonical rules y service reasoning. No hay una escalada unica que garantice agotar evidencia antes de bloquear.
- **Does missing GL trigger blocking review?** Si tras `canonicalize_normalized_invoice`; no esta garantizado para artefactos legacy que eviten esa capa.
- **Does the UI disable export when GL is blank?** No de manera general. Bulk/Billing V2 habilitan por existencia de rows; el backend moderno termina rechazando.
- **Is GL required in backend or only frontend?** Es requerido en backend moderno. El problema es cobertura de rutas, no ausencia total del requisito.

### Defecto de contrato interno

`validate_ai_extraction` calcula inicialmente `required_fields_present` solo con vendor, invoice number, invoice date, total y line items. Omite property, GL, due date, descriptions, URL y otros campos. La capa canonica lo corrige despues. Este doble significado permite que codigo intermedio o UI consuma una verdad incompleta.

## GL Reasoning Audit

### Autoridades actuales

1. GL propuesto por AI en cada line item.
2. Learned mapping por vendor/patron.
3. Vendor default GL.
4. Keyword ranking de `ai_mapping_review.gl_candidates` sobre chart.
5. Canonical category/vendor rules.
6. `service_invoice_gl_reasoning` por semantica de linea.
7. Utility-specific classifiers/defaults.
8. Manual edit/correction.

No hay un ranking final unico que compare todas estas fuentes con precedencia, compatibilidad y provenance inmutable.

### Capacidades existentes

- Valida que el codigo exista y sea payable en varias rutas.
- Distingue materiales de labor/service para painting, cleaning, plumbing y otras familias.
- Detecta unit/location y unit-turn context en ciertos textos.
- Produce `selected_gl`, alternativas, rejected alternatives, evidence y confidence.
- Prefiere cuentas especificas usando `GL_META` y listas de prioridad.
- Tiene pruebas utiles para paint materials vs painting labor, cleaning y plumbing.

### Limites estructurales

- `GL_META` y `_preferred_gl_codes` son una taxonomia parcial hardcoded, no metadata completa del chart.
- El reasoner excluye utilities, trash, marketing y subscriptions; esas categorias dependen de otras implementaciones.
- La clasificacion se basa principalmente en tokens/phrases y perfil de vendor; no representa obligacion economica, capitalizacion, contrato, recurrencia y contexto de propiedad como entidades tipadas.
- El primer GL compatible de una prioridad artesanal puede ganar sin una comparacion global contra todo el catalogo.
- AI extraction y GL suggestion ocurren en el mismo prompt; un error de lectura puede contaminar la clasificacion contable.
- Learned mappings y vendor defaults pueden dominar antes de una reevaluacion semantica robusta.
- Rejected alternatives pueden ser duplicados de alternatives y contener razones genericas.

### Ejemplos tecnicos observados

1. `tk_elevator`: la descripcion de la linea `Trip Charge` se reemplaza por el resumen global. El GL sigue reconciliando, pero se pierde evidencia line-level; un reasoner posterior razonaria sobre texto alterado.
2. La provenance de ese fixture muestra AI confidence 0.93, GL confidence 0.72 y `trade_family=unknown`; aun asi `review.required=false`. Es una decision contable de confianza media tratada como lista.
3. En el mismo fixture, la primera linea usa `canonical_service_accounting_reasoning`; la segunda conserva `ai_validated` sin `gl_accounting_reasoning`. Dos lineas del mismo invoice llegan por autoridades diferentes.
4. La alternativa 6615 usa el texto generico “less precise” sin una incompatibilidad contable verificable.

### Respuestas criticas

| Pregunta | Respuesta |
|---|---|
| Clasifica semantica antes de GL | Si, solo en rutas/categorias cubiertas por service reasoner. |
| Distingue materials vs labor/service | Si parcialmente; las pruebas focalizadas pasan. |
| Distingue recurring/fee/subscription | Parcial, en invoice nature/categorias y keywords; no como ontologia universal. |
| Distingue utilities de service invoices | Hay guardrails y evidencia utility; sigue distribuido entre varios procesadores. |
| Distingue unit-turn/remodel | Parcial por keywords/contexto. |
| Prefiere GL especifico | Si mediante prioridades/metadata parcial, no ranking completo. |
| Selecciona solo por vendor default | Puede usar vendor default con score alto en candidate mapping. |
| Deja GL blank con evidencia | Puede ocurrir cuando la familia no esta cubierta o las autoridades discrepan; el sistema bloquea pero no escala siempre a strong reasoning. |
| Muestra alternativas irrelevantes | El codigo intenta filtrarlas, pero la taxonomia parcial y razones genericas no lo garantizan. |
| Dice que no hay keyword aunque existe | Posible cuando descripcion canonica reemplaza source text o la familia no incluye ese token. |

## Semantic Classification Audit

Existe un clasificador semantico real, pero **no es general ni es el contrato central**. `classify_line_item_semantics()` retorna trade family, work mode, unit-turn context, repair/capital ambiguity, assets, location, indicators y confidence basis. Es una buena semilla.

### Cobertura actual aproximada

- Trade: cabinets, appliance, plumbing, electrical, HVAC, flooring, painting, cleaning, landscaping, pest, legal, utility, remodel y general maintenance.
- Work mode: materials, labor/service y casos ambiguos.
- Asset/location: algunos activos y patrones de unit.
- Context: vendor profile, invoice text, historical mapping y property.

### Lo que falta

- `document_family` tipado y obligatorio para todos los routes.
- `line_family` separado de trade y GL.
- recurrencia, fee, tax, credit, insurance, legal, marketing y finance como modos consistentes.
- hechos negativos y contradicciones (por ejemplo, “payment received” no payable).
- evidencia enlazada a pagina/span/bbox.
- version de clasificador y version de catalogo GL.
- calibracion por clase y confusion matrix.
- aplicacion obligatoria antes de toda seleccion GL.

### Contrato recomendado, no implementado

```json
{
  "document_family": "invoice",
  "line_family": "labor_service",
  "trade_family": "plumbing",
  "work_mode": "labor_service",
  "specific_assets": ["supply_line"],
  "location_detected": "7",
  "recurrence": "one_time",
  "semantic_confidence": 0.86,
  "evidence": [
    {"source": "page_1_text", "span": "repair leaking supply line", "page": 1}
  ],
  "contradictions": []
}
```

## Deterministic vs AI Routing Audit

### Ruta actual vs ideal

| Document type | Current route | Ideal route | Gap |
|---|---|---|---|
| Known digital utility PDF | detector -> deterministic processor | mismo + quality/output contract | fallback ocurre por cero output, no por output incompleto/incorrecto |
| Known vendor changed layout | deterministic; AI solo si falla de forma detectable | parser version detection -> facts validator -> selective AI | no layout fingerprint ni drift score universal |
| Clean unknown PDF | ingestion -> AI text | fast extraction -> semantic/accounting validator | extraction y GL suggestion mezclados |
| Scanned PDF | OCR/quality -> Vision si recomendado | cheap quality/OCR -> Vision en paginas relevantes | default Vision max 2 paginas puede omitir evidencia |
| Photo/screenshot | Vision si habilitado; OCR fallback | Vision-first con OCR auxiliar | proveedor/modelo puede no ser suficiente; no benchmark por calidad |
| Handwritten invoice | Vision | stronger vision extraction + strong reasoning | no handwriting-specific quality/escalation contract |
| Multi-invoice/master bill | segmentation regex + route | document graph/segmenter con reconciliation por cuenta | reglas ad hoc y riesgo de mezcla de paginas |
| Ambiguous GL | deterministic heuristics/candidates | strong accounting reasoner sobre facts + catalog subset | no strong-model route independiente |
| Low confidence/missing required | repair, Vision retry o review | escalation policy por campo y costo | no state machine unica de escalada |

### Hallazgos

- Deterministic-first existe y cubre muchos vendors.
- `batch_processor` solo activa AI fallback automaticamente si el procesador produce cero invoices/review/errors. Un resultado no vacio pero incompleto puede evitar AI fallback.
- Vision se activa por tipo/calidad/modo y puede reintentarse tras validacion de text AI.
- No existe un strong reasoning model separado para GL/accounting ambiguity.
- Las decisiones quedan parcialmente en `extraction_mode`, warnings y provenance, pero no en un route trace unico con razon y alternativas.
- La cola es FIFO global con un worker; batches concurrentes esperan en serie.

## Prompt and Model Usage Audit

### Configuracion efectiva observada

| Purpose | Provider | Model | Configuracion |
|---|---|---|---|
| Text extraction | OpenAI-compatible | `deepseek-v4-flash` | temperature 0; JSON object; max 4096 tokens; timeout 45 s; texto 45k chars; hasta 5 paginas |
| Vision extraction | OpenAI-compatible | `gemini-2.5-flash-lite` | JSON object; max 4096 tokens; timeout 45 s; hasta 2 paginas; 1600 px |
| GL reasoning | No LLM separado | deterministic semantic heuristics + candidate rules; AI extraction puede sugerir GL | sin model/temperature propios |
| Fallback reasoning | mismo extraction route o manual review | no strong reasoner dedicado | Vision puede reintentarse; no escalada GPT-5.6 |

No se encontro telemetria consistente de costo por provider/model. Las latencias si se registran parcialmente.

### Prompts

| Prompt | Model | Purpose | Schema/controls | Weakness | Risk |
|---|---|---|---|---|---|
| `_build_prompt` | deepseek-v4-flash | extraer invoice completo y sugerir GL | JSON object, temperature 0, schema manual, repair retry | mezcla facts, document class, descriptions, property y GL; refs truncadas | High |
| `_build_vision_prompt` | gemini-2.5-flash-lite | lectura visual + bboxes + misma semantica | JSON object, candidates/bboxes, max 2 pages | mismo acoplamiento; paginas relevantes pueden quedar fuera | High |
| `_repair_prompt` | mismo modelo | reparar JSON/schema | exige todas las keys | reenvia prompt completo; duplica latencia y no garantiza JSON Schema | Medium |
| Canonical rules summary | incluido en ambos | guiar policies | resumen textual | reglas textuales compiten con backend determinista | Medium |
| AI mapping review | no LLM principal | candidatos vendor/property/GL | fuzzy/keyword/history scores | vendor default 0.87 y AI valid GL 0.92 pueden preceder semantica profunda | High |

### Evaluacion de prompt

- Obliga JSON, confidence, razones, reconciliacion y no inventar valores.
- Pide GL solo del chart, pero permite `gl_account_candidate=""`; eso es correcto para extraction, no para readiness final.
- Pide evidencia breve por linea, pero no rejected alternatives contables verificables.
- No separa extraction confidence de GL confidence.
- No usa JSON Schema Structured Outputs; valida manualmente despues.
- Incluye referencias seleccionadas, pero tambien corta muestras y texto por limites; no hay garantia de que el GL/property correcto este en el contexto enviado.
- No hay prompt especializado que reciba solo hechos verificados y decida entre 3-10 candidatos compatibles.
- No hay adversarial instruction boundary explicita para tratar texto del invoice como datos no confiables; un documento podria contener prompt-like text.

## GPT-5.6 / Strong Model Recommendation

La documentacion oficial actual lista GPT-5.6 Sol como modelo flagship, GPT-5.6 Terra como balance costo/inteligencia y GPT-5.6 Luna para volumen/costo. La disponibilidad documentada no implica que las credenciales/proveedor OpenAI-compatible actuales lo tengan habilitado. Fuentes: [modelos OpenAI](https://developers.openai.com/api/docs/models), [GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra), [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna).

### Recomendacion dirigida

| Task | Current model/route | Recommended model/route | Why |
|---|---|---|---|
| Known clean utility | deterministic parser | deterministic, sin LLM | menor costo, mayor repetibilidad |
| Clean unknown invoice facts | deepseek-v4-flash | mantener fast model si benchmark cumple | la tarea es extraction, no reasoning complejo |
| Scan/photo facts | gemini-2.5-flash-lite | vision benchmark winner; evaluar GPT-5.6 vision si disponible | calidad debe medirse por field accuracy, no marca |
| Semantic classification clara | deterministic classifier | deterministic ontology/rules | explicable y rapida |
| GL ambiguo con candidatos validos | heuristics/current extraction AI | GPT-5.6 Sol o strong model equivalente, solo fallback | requiere comparar evidencia, politica y alternativas |
| Ambiguedad media a volumen | no route | GPT-5.6 Terra, si benchmark supera fast model | balance costo/precision |
| Rule candidate generation | no route formal | strong reasoner, output solo como proposal | sintetiza patrones; nunca activar sin approval/backtest |
| Final readiness | fragmentada | determinista, no LLM | un modelo no debe autorizar exportacion |

**No se recomienda migrar todo a GPT-5.6.** Se recomienda una cascada con presupuesto: deterministic -> fast extraction/vision -> semantic validator -> strong reasoning solo si GL/property/document class siguen ambiguos. La decision de modelo debe basarse en benchmark, latencia y costo real.

## Confidence and Readiness Audit

### Estado actual

- El provider entrega una confidence global y por linea.
- Backend la acepta si existe, la deriva solo si falta y luego la limita por ciertos issues.
- GL reasoner calcula otra confidence independiente.
- Property/vendor tienen scores en subsistemas separados.
- `validation_summary.valid`, `required_fields_present`, review reasons y status de UI no comparten una unica formula.
- Billing V2 `ready` exige required columns y cero review reasons, pero Export no usa ese criterio para habilitarse.
- Single Invoice legacy construye un subconjunto de tasks y permite marcar algunas como resueltas localmente.

### Evidencia de calibracion

Muestreo de caches recientes:

- 143 rows AI.
- mediana de AI confidence: 0.95; rango 0.68-1.00.
- 110/143 rows AI tenian flags de review.
- 2 rows AI con GL vacio y 8 con property vacia.

Una confidence mediana de 95% coexistiendo con aproximadamente 77% de rows flagged no es una confidence operativa calibrada. Describe, como mucho, seguridad subjetiva del extractor.

### Modelo recomendado

```json
{
  "extraction_confidence": 0.95,
  "document_classification_confidence": 0.88,
  "vendor_mapping_confidence": 0.90,
  "property_mapping_confidence": 0.92,
  "line_item_confidence": 0.86,
  "gl_reasoning_confidence": 0.40,
  "total_reconciliation": "passed",
  "required_fields": "failed",
  "accounting_readiness": "needs_review",
  "blocking_reasons": ["line_2.gl_account_missing"]
}
```

Reglas recomendadas:

1. Confidence nunca sustituye un contract check.
2. `Ready` requiere required fields, valid chart accounts, reconciliation, duplicate decision y cero blockers.
3. Una reparacion inferida reduce confidence del campo reparado, no solo agrega warning.
4. Confidence debe calibrarse con reliability curves por campo/ruta/vendor family.
5. `export_allowed` debe emitirse solo por un servicio determinista de readiness.

## UI/UX Reasoning Audit

### Lo que ya funciona

- GL explanation accesible por hover/focus y teclado.
- Source/trace/vision assist y document-page synchronization.
- Bulk y Single Invoice.
- Correccion inline y mapeos aprendidos.
- Filtros de missing required/review/AI en Billing V2.
- Local loading/progress y viewer detached.

### Gaps de confianza y operacion

1. El popup explica una decision, pero la provenance puede venir de distintas autoridades y no siempre contiene rejected alternatives reales.
2. No hay una distincion visual estable entre extraction-ready y accounting-ready.
3. Bulk Export se habilita con rows aunque haya blockers; el error aparece tarde desde backend.
4. Legacy Single y Billing V2 calculan readiness de forma distinta.
5. El usuario no ve en una sola superficie todos los campos que bloquean exportacion.
6. “Resolved” local puede ocultar un task sin registrar evidencia persistente de resolucion.
7. No siempre se muestra claramente `deterministic`, `AI text`, `AI vision`, cache hit o strong fallback por invoice/linea.
8. El grid de 24 columnas aumenta carga cognitiva; no existe una vista de “blockers first” unificada.
9. Billing V2 carga aproximadamente 337 `<option>` de batches en el DOM; QA/smoke y datos operativos aparecen mezclados.
10. La busqueda de Billing V2 no esta debounced y recorre todas las rows en cada tecla.
11. En el viewport auditado hubo 1,616 elementos, 337 options y 11 canvas aun con solo 12 rows visibles.
12. Componentes principales y CSS son excesivamente grandes: `App.tsx` 4,492 lineas, `TemplateWorkspace.tsx` 3,707 y `styles.css` 20,743.

## Self-Training / Rules Audit

### Capacidades actuales

- Persistencia de corrections por vendor en `webapp_data`.
- `value_override` y `region_remap`.
- Learned vendor/GL/property mappings.
- Correccion manual puede reutilizarse en ejecuciones futuras.
- Canonical/vendor rules editables y test bench parcial.

### Lo que no constituye self-training seguro

- No hay layout fingerprint/version.
- No hay estado `candidate -> approved -> active -> deprecated`.
- No hay autor, razon, fecha efectiva, modelo/rule version ni documento de evidencia obligatorios.
- No hay deteccion de conflictos entre regla historica, vendor default y semantica actual.
- No hay backtest automatico contra documentos historicos antes de activar.
- No hay metricas de blast radius, rollback atomico ni expiry.
- Una correccion poco especifica puede volverse vendor-wide.
- Region remap tiene alcance limitado y comentarios orientados a casos concretos.

Diagnostico: es un **correction store**, no un sistema de aprendizaje gobernado.

## Benchmark Plan

### Estructura propuesta

```text
tests/fixtures/document_benchmark/
  manifest.csv
  expected_outputs.json
  documents/
    digital/
    scans/
    photos/
    handwritten/
  results/
    current/
    candidate_models/
```

`manifest.csv` debe incluir ID anonimizado, document family, quality tier, vendor family, expected route, page count, sensitive-data class y split fijo (`train/dev/test`). Los documentos no deben publicarse ni versionarse si contienen PII.

### Metricas obligatorias

- Exact match/normalized match para vendor, invoice number, dates, property y location.
- Precision/recall/F1 de line items y document/line classification.
- Amount accuracy y exact total reconciliation.
- GL fill rate y blank GL rate.
- GL correctness top-1 y top-k por linea.
- False Ready rate, false block rate y review precision.
- Route accuracy, retries, cache hits, time y costo estimado.
- Calibration error/Brier score por confidence field.
- Duplicate-risk accuracy.

### Cohortes minimas

Clean digital PDFs, scans, photos, handwriting, recurring utilities, material suppliers, contractors, renewals/fees/subscriptions, legal, finance/loan, past-due notices, statements, master bills y unknown documents.

### Scripts propuestos

```powershell
python scripts/run_document_reasoning_benchmark.py --sample 100 --model current
python scripts/run_document_reasoning_benchmark.py --sample 100 --model gpt-5.6
python scripts/compare_benchmark_results.py
```

### Gates sugeridos antes de produccion

- Required-field fill rate: 100% o explicit blocking explanation.
- Blank GL en exported rows: 0%.
- False Ready: 0% en gold set.
- Amount/total exact match: >=99.5% y 100% para batches exportados.
- GL top-1 correctness: objetivo inicial >=95%, con review para el resto.
- Document route accuracy: >=98%.
- Regression: ningun fixture gold complete puede fallar.

## Observability and Traceability Audit

### Existe

- `performance.json/jsonl` por batch.
- progress stages y current file/step.
- `_meta` por row con provider/model/mode/confidence/warnings/reasons.
- Vision trace con bbox, field, value, confidence y columns alimentadas.
- manual review/revisions/cache.
- AI fallback audit JSONL en ruta configurable.

### Falta

- ID estable de document/invoice/line/decision compartido por todas las capas.
- route reason completo y cada transicion de fallback.
- input hash, parser version, rule version, reference snapshot y model prompt version en una sola traza.
- hechos antes/despues de cada repair/override.
- ranking GL completo con scores por criterio.
- readiness decision y export authorization enlazadas al mismo decision ID.
- queue wait y cancellation latency confiables; algunos stages registrados como cero/coarse.
- costo/token usage estandarizado.
- politicas de retencion/redaccion para `detected_text`, account numbers y prompts/cache.

### Decision ledger recomendado

Cada decision debe ser append-only y registrar `source -> candidate -> transformation -> selected/rejected -> validator -> readiness`. La UI debe leer ese ledger; no reconstruir explicaciones a posteriori.

## Performance Audit

Muestra de los 40 archivos `performance.jsonl` mas recientes disponibles durante la auditoria:

| Route/stage | N | Current avg | Median / P95 | Max | Target | Bottleneck |
|---|---:|---:|---:|---:|---:|---|
| `batch.total` | 30 | 41.5 s | 25.7 s / 74.5 s | 471.5 s | known batch <10 s; mixed proportional | serial routing/OCR/provider/processors |
| `vendor.detect_all` | 30 | 6.9 s | n/a / 29.9 s | 41.6 s | <1 s digital; <3 s OCR | ingestion/OCR durante deteccion |
| `ai.text_call` | 41 | 11.1 s | n/a / 22.5 s | 30.3 s | <8 s median | provider + retries + prompt size |
| `ai.vision_call` | 10 | 5.1 s | n/a / 19.3 s | 19.3 s | <10 s median | rendering/provider/repair |
| `preview.normalize` | 56 | 154 ms | n/a / 773 ms | 1.08 s | <100 ms incremental | renormaliza cache completo |
| `manual_review.normalize` | 52 | 167 ms | n/a / 685 ms | 1.32 s | compartir normalized view | trabajo duplicado |

### Causas principales

- Vendor detection puede disparar ingestion/OCR antes del route final.
- Cola global de un worker serializa batches.
- Retries HTTP (2 text, 3 vision) multiplican timeout de 45 s.
- Prompt amplio y referencias/texto grandes.
- Preview y manual review leen y renormalizan el mismo resultado por separado.
- Procesadores monoliticos reportan stages como iniciados/completados alrededor de una llamada, no tiempos internos reales.
- Billing V2 mantiene todos los batch options y filtra rows sin debounce.

### Caching

Hay cache AI por hash del payload y cache de preview/ingestion, pero el key AI incluye el prompt completo y referencias; cambios menores invalidan. Falta una cache de hechos por file hash independiente de prompt/model y una cache separada de accounting decisions por facts hash + rule/catalog version.

## Security and Privacy Audit

| Risk | Evidence | Severity | Mitigation |
|---|---|---:|---|
| Backend sin auth/authz | routers incluidos sin dependency de identidad | Critical si sale de localhost | OAuth/session, RBAC, tenant isolation |
| Raw document exposure | endpoints por batch/file sirven contenido | Critical si red accesible | authorization por tenant/document, signed short-lived URLs |
| Mutations sin authorization | process, cancel, edit, delete, export | Critical si red accesible | RBAC + CSRF/origin controls + audit identity |
| Upload sin byte quota | valida extension, no size limit global | High | per-file/batch quotas, streaming limits, decompression/page limits |
| MIME/content mismatch | extension allowlist | High | magic-byte detection, sandbox converters |
| PII hacia providers | texto y page images enviados a providers configurados | High | DPA/retention settings, redaction where possible, disclosure/consent |
| Sensitive cache/trace | AI payload-derived cache y `detected_text` en trace | High | encryption at rest, retention, access control, redacted logs |
| Prompt injection documental | invoice text entra al user prompt | High | explicit data boundary, schema-constrained tools, post-validation |
| Test/runtime mixing | QA/smoke batches visibles con operativos | Medium | separate environments/data roots |
| Secrets | `.env` ignorado; keys no aparecen en API auditada | Low actual | secret manager, rotation, startup validation |

CORS solo permite 5173 mientras el frontend observado usa 5174; eso es configuracion obsoleta, no una barrera de seguridad. CORS no reemplaza autenticacion.

## Root Causes

1. **No single source of truth:** extraction, canonicalization, mappings, vendor rules, GL reasoner, utilities y UI mantienen contratos paralelos.
2. **Facts y accounting decisions estan acoplados:** el mismo prompt intenta leer y categorizar.
3. **Readiness no es una entidad:** se deriva varias veces desde subsets distintos de flags/campos.
4. **Fallback basado en zero-output:** no evalua universalmente completitud/correctitud de output no vacio.
5. **Semantica parcial:** existe como helper de service invoices, no como paso obligatorio.
6. **Confidence no calibrada:** confianza declarada por modelo se presenta cerca de estados operativos.
7. **Legacy paths sobreviven:** export y procesadores pueden evitar contratos modernos.
8. **Correcciones sin governance:** aprendizaje local puede ampliar una decision demasiado.
9. **Observabilidad por artefactos, no por decision:** dificulta explicar que capa cambio un valor.
10. **Testing orientado a casos, no a distribucion:** muchos smokes, pocos gold documents y sin metricas de precision/false Ready.

## Severity-ranked Issues

## Issue: Exportacion no tiene un unico gate accounting-ready

Severity: Critical

Evidence:
- file/function: `batch_processor.export_batch`, `TemplateWorkspace`, `BillingV2.exportBatch`.
- observed behavior: las rutas modernas validan rows; el fallback legacy puede copiar un workbook. Bulk/Billing V2 habilitan Export por existencia de rows, no por blockers.
- reproduction path: invocar export sin `edited_rows` sobre un batch legacy sin preview canonico util, o hacer click en Bulk con review pendiente.

Root cause:
- Coexisten contratos de exportacion modernos y legacy; la UI no consume una autorizacion backend unica.

Impact:
- Puede producir un XLSX que parece ResMan-ready sin haber pasado el mismo contract check de GL/property/total/URL.

Recommended fix:
- Un solo `AccountingReadinessService.authorize_export(snapshot_id)` obligatorio para toda ruta; eliminar copy bypass.

Risk if not fixed:
- Errores contables, imports rechazados y perdida de confianza.

Test required:
- Contract test que intente exportar cada campo requerido vacio, GL invalido, total mismatch y legacy workbook.

## Issue: Ready y Needs Review tienen definiciones incompatibles

Severity: Critical

Evidence:
- file/function: `canonical_rules._apply_required_field_validation`, `TemplateWorkspace.buildReviewTasks/deriveRequiredReviewFlags`, `BillingV2.rowPassesFilter`.
- observed behavior: canonical, legacy Single y Billing V2 derivan readiness de conjuntos diferentes; tasks pueden resolverse localmente.
- reproduction path: cargar una row con due date/descripcion/URL faltante y comparar status en vistas.

Root cause:
- Readiness no esta persistida como decision backend versionada.

Impact:
- False Ready o UX contradictoria; el usuario descubre el bloqueo al exportar.

Recommended fix:
- Backend debe emitir por invoice/row un readiness object exhaustivo; UI solo lo representa.

Risk if not fixed:
- Output invalido y review incompleto.

Test required:
- Matriz de cada blocker x Bulk/Single/Billing V2/API export.

## Issue: GL obligatorio no esta garantizado en todas las rutas

Severity: Critical

Evidence:
- file/function: canonical required validation, output validator y legacy export fallback.
- observed behavior: modern rows bloquean blank GL, pero runtime sample conserva rows con GL vacio y legacy path no comparte el gate.
- reproduction path: procesar documento ambiguo que no resuelve GL y exportar por una ruta no-row/legacy.

Root cause:
- Cobertura de contrato por ruta, no falta de una regla aislada.

Impact:
- ResMan row invalida o gasto sin clasificacion.

Recommended fix:
- GL resolution state obligatorio (`resolved|blocked`) y export authorization central.

Risk if not fixed:
- Misclassification o import incompleto.

Test required:
- Property-based tests que ninguna exported row tenga GL blank/no-chart.

## Issue: No existe una autoridad unica de razonamiento GL

Severity: High

Evidence:
- file/function: `ai_provider`, `ai_mapping_review`, `canonical_rules`, `service_invoice_gl_reasoning`, utility processors, learned corrections.
- observed behavior: distintas lineas del fixture TK usan `canonical_service_accounting_reasoning` y `ai_validated`; una carece de reasoning object.
- reproduction path: invoice con varias lineas y señales mixtas/default vendor.

Root cause:
- Crecimiento incremental por capas sin decision protocol comun.

Impact:
- Resultados inconsistentes, alternativas genericas y explicaciones no comparables.

Recommended fix:
- Semantic-first candidate ranker unico sobre chart metadata, con adapters para reglas deterministas y AI.

Risk if not fixed:
- Nuevos vendors aumentaran excepciones y regresiones.

Test required:
- Gold set line-level con top-1/top-k y rejected alternatives.

## Issue: Extraccion y accounting reasoning estan acoplados en el mismo prompt

Severity: High

Evidence:
- file/function: `ai_provider._build_prompt/_build_vision_prompt`.
- observed behavior: un mismo JSON pide facts, class, descriptions, property y GL candidate.
- reproduction path: documento con OCR ambiguo y GL semanticamente complejo.

Root cause:
- Falta de contratos separados `DocumentFacts` y `AccountingDecision`.

Impact:
- Alucinacion o error de lectura contamina GL y confidence.

Recommended fix:
- Dos etapas: extraction schema constrained; despues reasoning solo con facts verificados/candidatos.

Risk if not fixed:
- Precision limitada aun cambiando de modelo.

Test required:
- A/B extraction-only vs combined prompt con field accuracy y GL correctness.

## Issue: Confidence global no representa accounting readiness

Severity: High

Evidence:
- file/function: `validate_ai_extraction`, row `_meta`, runtime cache sample.
- observed behavior: mediana AI confidence 0.95 mientras 110/143 AI rows estan flagged; fixture TK tiene 0.93 extraction y 0.72 GL sin GL review.
- reproduction path: AI output con confidence alta y mapping/property issue.

Root cause:
- Confidence provider aceptada y caps parciales; no calibracion por campo/decision.

Impact:
- Usuario sobreconfia en output no listo.

Recommended fix:
- Confidence vector calibrado y readiness determinista independiente.

Risk if not fixed:
- False Ready y errores silenciosos.

Test required:
- Calibration curves, Brier score y false-ready test set.

## Issue: Fallback AI no detecta todo output determinista incompleto

Severity: High

Evidence:
- file/function: `batch_processor.process_batch` fallback condition.
- observed behavior: fallback se activa principalmente cuando un procesador produce cero invoices/review/errors, no ante nonzero incompleto.
- reproduction path: parser conocido retorna una invoice pero omite segunda cuenta/linea/property/GL.

Root cause:
- Fallback basado en existencia, no en output quality contract.

Impact:
- Omisiones parciales parecen exito determinista.

Recommended fix:
- Evaluar cada output contra document facts, expected segments, totals y required/readiness antes de aceptar route.

Risk if not fixed:
- Facturas parcialmente procesadas y montos faltantes.

Test required:
- Mutations de layout y master bills con line/page count expectations.

## Issue: Semantic classifier no es universal ni obligatorio

Severity: High

Evidence:
- file/function: `service_invoice_gl_reasoning.classify_line_item_semantics`, category exclusions.
- observed behavior: cobertura util de services, pero utilities/marketing/subscriptions usan otras reglas y unknown trade sigue comun.
- reproduction path: fee/renewal/legal/finance o mixed materials-service fuera de families.

Root cause:
- Taxonomia implementada como helper local, no domain model.

Impact:
- GLs genericos, alternativas irrelevantes y escalada excesiva a review.

Recommended fix:
- Ontologia universal document/line/work mode/asset con evidence y contradictions.

Risk if not fixed:
- Hardcoding crece mas rapido que generalizacion.

Test required:
- Confusion matrix por document/line family y compatibility tests GL-semantic.

## Issue: Regresion activa en canonical fixture

Severity: High

Evidence:
- file/function: `smoke_canonical_invoice_fixtures.py`, fixture `tk_elevator`.
- observed behavior: FAIL; `Trip Charge` es reemplazado por descripcion global.
- reproduction path: ejecutar el smoke canonico.

Root cause:
- Composicion de descripcion line-level/global sin invariantes de preservacion.

Impact:
- Evidencia semantica de linea degradada y output no esperado.

Recommended fix:
- Corregir solo despues de aislar stage y agregar invariant tests.

Risk if not fixed:
- Descripciones engañosas y GL reasoning sobre texto incorrecto.

Test required:
- Fixture debe pasar y verificar source-line preservation.

## Issue: Learned corrections carecen de governance

Severity: High

Evidence:
- file/function: `learned_corrections.py`, learned mappings.
- observed behavior: overrides/remaps por vendor sin lifecycle, backtest, versionado o rollback formal.
- reproduction path: correction con scope poco especifico aplicada a otro layout del mismo vendor.

Root cause:
- Persistencia diseñada como convenience store.

Impact:
- Una correccion puede institucionalizar un error.

Recommended fix:
- Rule proposal registry con scope/fingerprint, approval, historical test, metrics y rollback.

Risk if not fixed:
- Regresiones cross-document dificiles de rastrear.

Test required:
- Conflict/scope/rollback tests con dos layouts del mismo vendor.

## Issue: API local no tiene controles de identidad ni tenancy

Severity: High

Evidence:
- file/function: `main.py` y routers.
- observed behavior: no auth dependency; raw file, edit, delete, process y export endpoints.
- reproduction path: acceder al backend desde cualquier cliente con batch ID valido.

Root cause:
- Arquitectura nacida como herramienta desktop/local.

Impact:
- Exposicion de PII/financial data y operaciones no autorizadas.

Recommended fix:
- Antes de Outlook/cloud: auth, RBAC, tenant isolation, audit identity y retention.

Risk if not fixed:
- Incidente de seguridad severo.

Test required:
- Authorization matrix y tenant isolation penetration tests.

## Issue: Observabilidad no reconstruye una decision end-to-end

Severity: Medium

Evidence:
- file/function: performance JSONL, row meta, vision trace, review/revisions.
- observed behavior: artefactos separados; stages monoliticos y algunos tiempos cero.
- reproduction path: intentar responder que capa cambio un GL despues de reprocess/correction.

Root cause:
- Logs agregados por feature, no por domain decision ID.

Impact:
- Debug lento y explicaciones incompletas.

Recommended fix:
- Decision ledger append-only por document/invoice/line.

Risk if not fixed:
- Coste operativo alto y poca auditabilidad.

Test required:
- Trace completeness contract para cada route.

## Issue: Test suite no es un benchmark ejecutable y estable

Severity: Medium

Evidence:
- file/function: canonical fixtures, backend tests, Billing V2 smoke.
- observed behavior: 5 complete fixtures, 1 skipped; uno falla. `unittest` descubre 0 tests; `httpx` falta.
- reproduction path: comandos de validacion listados al inicio.

Root cause:
- Smokes incrementales sin harness/dependencies/metrics unificados.

Impact:
- No se puede demostrar mejora general ni comparar modelos.

Recommended fix:
- Benchmark gold versionado y CI hermetico con field/GL/readiness metrics.

Risk if not fixed:
- “Mejoras” locales causan regresiones invisibles.

Test required:
- CI desde entorno limpio y benchmark comparison gate.

## Issue: UI y API realizan trabajo ansioso/repetido

Severity: Medium

Evidence:
- file/function: Billing V2 select/search, preview/manual review endpoints.
- observed behavior: 337 batch options; search sin debounce; preview/review renormalizan por separado.
- reproduction path: abrir Billing V2 con historial grande y escribir rapidamente.

Root cause:
- Datos globales cargados en superficie principal y views derivadas independientes.

Impact:
- Latencia, DOM grande y menor fluidez.

Recommended fix:
- Paginated/virtual batch selector, debounced search, normalized snapshot compartido.

Risk if not fixed:
- Degradacion progresiva con volumen.

Test required:
- Browser benchmark con 500 batches y 500/5,000 rows.

## Recommended Fix Plan

### Phase 1: Guardrails

- Crear `AccountingReadiness` backend unico.
- Prohibir toda exportacion con blank/invalid GL, property, amount, descriptions, dates, URL o reconciliation failed.
- Retirar/bloquear legacy workbook copy sin validacion.
- Persistir blockers y resolution evidence; UI no debe inventar Ready.
- Unificar required-field contract y su schema.
- Completar decision/provenance IDs y arreglar harness de tests.
- Gate de CI: todos los fixtures complete pasan; false Ready 0.

### Phase 2: Universal Semantic GL Engine

- Definir ontologia document/line/trade/work mode/assets/recurrence.
- Crear metadata completa del chart: family, mode, scope, incompatibilities, specificity.
- Separar facts de decisions.
- Rankear candidates con evidencia positiva, negativa y policy.
- Guardrails materials vs contracted service, utility vs repair, recurring vs one-time.
- Strong review solo cuando top candidates no superen margen/calibracion.

### Phase 3: AI/Model Routing

- Extraction schema estricto y versionado.
- Fast model para facts simples; Vision solo por quality/layout evidence.
- Strong reasoner (GPT-5.6 Sol/equivalente) solo para accounting ambiguity.
- Terra/equivalente para middle tier si benchmark lo justifica.
- Time/cost budget, cache por facts hash y model/prompt version.
- A/B benchmark antes de cambiar produccion.

### Phase 4: Self-Training

- Capturar corrections como rule candidates, no overrides activos inmediatos.
- Scope por vendor + layout fingerprint + semantic pattern + property policy.
- Approval humano, backtest historico y regression report.
- Versionado, conflict detection, canary, rollback y expiry.
- Medir precision de cada rule y desactivarla automaticamente ante drift.

### Phase 5: Email/Outlook Agent

- Solo despues de false Ready 0 y auth/tenancy completos.
- Microsoft OAuth con least privilege.
- AP inbox classifier, dedupe, retention y quarantine.
- Daily briefing con blockers, no auto-export sin readiness authorization.

## 30/60/90 Day Technical Roadmap

### 0-30 dias

- Congelar expansion de vendor hardcodes salvo defectos contables criticos.
- Implementar diseño del readiness contract y decision ledger.
- Cerrar todas las rutas de exportacion bajo el mismo gate.
- Reparar suite: `httpx`, discovery, TK fixture y entorno CI limpio.
- Construir 100-document gold benchmark anonimizado.
- Separar QA/runtime data roots.
- Definir politica de datos/retencion/provider.

### 31-60 dias

- Introducir semantic domain model y adapters para processors actuales.
- Crear chart metadata completo y candidate ranker.
- Ejecutar benchmark current vs fast/vision/strong candidates.
- Calibrar confidence por campo/ruta.
- Unificar UI blockers/readiness y explanation desde ledger.
- Optimizar snapshot normalization, vendor detection y queue concurrency segura.

### 61-90 dias

- Activar strong reasoning fallback con budget/gates.
- Lanzar governed rule candidates y historical backtest.
- Expandir benchmark a 500+ documentos y drift suites.
- Añadir auth/RBAC/tenant isolation si habrá despliegue compartido.
- Piloto controlado con shadow mode: decisiones comparadas, sin auto-export nuevo.
- Solo tras cumplir gates, diseñar Outlook agent.

## Open Questions

1. ¿InnerView seguira siendo estrictamente local/desktop o se desplegara en red/cloud?
2. ¿Cual es la fuente autoritativa del chart de cuentas y su version/fecha efectiva?
3. ¿Accounting Date debe seguir invoice date o periodo contable configurable por propiedad/cliente?
4. ¿Que campos son legalmente opcionales en ResMan frente a politicamente obligatorios en NexGen?
5. ¿Un Document Url local es aceptable temporalmente o export requiere siempre Dropbox?
6. ¿Quien aprueba reglas aprendidas y quien puede hacer override de GL/readiness?
7. ¿Cual es el costo maximo por invoice y SLA por route?
8. ¿Que providers tienen DPA/no-training/retention adecuados para invoices reales?
9. ¿Existe un historial de exports corregidos que pueda convertirse en gold labels con auditoria?
10. ¿Como debe tratarse un invoice con total reconciliado pero clasificacion GL incierta: block siempre o escalation fuerte automatica?
11. ¿Debe la app soportar multiples clientes/COA/politicas, o solo una configuracion NexGen?
12. ¿Cual es la politica de duplicates: exact invoice key, fuzzy document hash, account-period-total o combinacion?

## Final Assessment

InnerView tiene activos tecnicos valiosos y una base recuperable. La debilidad no es falta de codigo ni falta de AI; es exceso de autoridades sin un contrato central. El siguiente avance de calidad no debe ser “otro parser” ni “otro prompt”. Debe ser una columna vertebral de facts, semantica, accounting decision y readiness que todos los parsers/modelos/UI/export consuman.

Un modelo fuerte como GPT-5.6 puede elevar el razonamiento en el ultimo 5-15% ambiguo. No puede convertir por si mismo una pipeline fragmentada en un sistema accounting-grade. Primero deben quedar innegociables los contracts, trazas y benchmarks; despues el modelo fuerte se vuelve una ventaja competitiva medible en lugar de una fuente adicional de variabilidad.

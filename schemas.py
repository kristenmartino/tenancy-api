"""
Pydantic schemas for residential lease extraction.

Anchored on the Texas Apartment Association (TAA) template, generalized to
support NAA and common state variants (CA, FL). Every field is wrapped in
ExtractedField, which carries provenance (page + char span) and a confidence
score. This is the contract between the LangGraph extraction nodes and the
review UI / MCP server.
"""

from datetime import date
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Provenance wrapper
# ---------------------------------------------------------------------------

class BoundingBox(BaseModel):
    """Normalized (0.0-1.0) page coordinates of one rectangular highlight.

    All four values are fractions of the page's width / height, so the same
    coords work regardless of render resolution. (x, y) is the top-left
    corner; origin is the page's top-left.

    For multi-line highlights, emit one `BoundingBox` per line — see
    `SourceSpan.bboxes`. This matches the PDF spec's `/Highlight` annotation
    QuadPoints model (Adobe, Mendeley render highlights the same way).
    """
    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)
    width: float = Field(..., gt=0.0, le=1.0)
    height: float = Field(..., gt=0.0, le=1.0)


SourceMatchType = Literal[
    "filled",     # value is typed/printed in a writable space
    "blank",      # there's a labeled placeholder but no value filled in
    "inferred",   # value is implied by surrounding prose, not a fillable field
    "checkbox",   # value is decided from a checked/unchecked box
    "absent",     # document doesn't address this field at all
]


class SourceSpan(BaseModel):
    """Where in the source PDF this field was extracted from.

    `bboxes` is the load-bearing field — one rect per line, derived
    server-side by aligning `snippet` against the OCR text layer's
    per-word coordinates (see `bbox.py`). Empty list means "no overlay,
    just navigate to the page". `match_type` discriminates how the field
    was found, which gates which derivation path applies.

    `bbox` (singular, deprecated) is preserved for one schema cycle so
    in-flight DB rows from the prior single-rect contract still validate.
    """
    page_number: int = Field(..., ge=1, description="1-indexed page number")
    char_start: int = Field(..., ge=0, description="Character offset on page")
    char_end: int = Field(..., ge=0, description="Character offset on page")
    snippet: str = Field(..., description="Verbatim text supporting the extraction")
    match_type: SourceMatchType = Field(
        default="filled",
        description=(
            "How the extraction located this field. Drives which bbox "
            "derivation path runs (snippet-anchored OCR for filled / blank "
            "/ inferred; LLM-vision for checkbox; null for absent)."
        ),
    )
    section_label: str | None = Field(
        default=None,
        description=(
            "Human-readable section heading the field sits under, e.g. "
            "'3. Lease Term', 'Utilities and Services'. Cosmetic context "
            "for the review UI; not used in bbox derivation."
        ),
    )
    bboxes: list[BoundingBox] = Field(
        default_factory=list,
        description=(
            "Per-line highlight rectangles (PDF QuadPoints model). Empty "
            "list = no overlay. Derived from `snippet` against OCR word "
            "positions for text fields; from LLM-vision for checkboxes."
        ),
    )
    bbox: BoundingBox | None = Field(
        default=None,
        description=(
            "DEPRECATED. Single-rect form of the highlight, kept for one "
            "schema cycle to validate existing DB rows. New extractions "
            "populate `bboxes` instead. Remove after a clean re-extract."
        ),
    )


class ExtractedField[T](BaseModel):
    """Every extracted field carries provenance and confidence."""
    value: T | None
    confidence: float = Field(..., ge=0.0, le=1.0)
    source: SourceSpan | None = None
    notes: str | None = None  # Model reasoning, ambiguity flags, etc.


# ---------------------------------------------------------------------------
# Domain enums
# ---------------------------------------------------------------------------

class LeaseTemplate(StrEnum):
    TAA = "taa"  # Texas Apartment Association
    NAA = "naa"  # National Apartment Association
    CALIFORNIA = "ca_residential"
    FLORIDA = "fl_residential"
    UNKNOWN = "unknown"


class LeaseType(StrEnum):
    FIXED_TERM = "fixed_term"
    MONTH_TO_MONTH = "month_to_month"
    WEEK_TO_WEEK = "week_to_week"


class RolloverBehavior(StrEnum):
    AUTO_MONTH_TO_MONTH = "auto_month_to_month"
    REQUIRES_RENEWAL = "requires_renewal"
    TERMINATES = "terminates"


class UtilityResponsibility(StrEnum):
    TENANT = "tenant"
    LANDLORD = "landlord"
    SHARED = "shared"
    NOT_APPLICABLE = "not_applicable"


# ---------------------------------------------------------------------------
# Section models
# ---------------------------------------------------------------------------

class Party(BaseModel):
    name: ExtractedField[str]
    role: str  # "tenant" | "co_tenant" | "co_signer" | "landlord" | "property_manager"
    email: ExtractedField[str] | None = None
    phone: ExtractedField[str] | None = None
    address: ExtractedField[str] | None = None


class Property(BaseModel):
    street_address: ExtractedField[str]
    city: ExtractedField[str]
    state: ExtractedField[str]
    zip_code: ExtractedField[str]
    unit_number: ExtractedField[str] | None = None
    parking_spaces: ExtractedField[list[str]] | None = None
    square_feet: ExtractedField[int] | None = None


class Term(BaseModel):
    start_date: ExtractedField[date]
    end_date: ExtractedField[date]
    lease_type: ExtractedField[LeaseType]
    rollover: ExtractedField[RolloverBehavior] | None = None
    notice_to_vacate_days: ExtractedField[int] | None = None


class Rent(BaseModel):
    base_monthly_rent: ExtractedField[float]
    prorated_first_month: ExtractedField[float] | None = None
    due_day_of_month: ExtractedField[int]
    grace_period_days: ExtractedField[int] | None = None
    late_fee_flat: ExtractedField[float] | None = None
    late_fee_daily: ExtractedField[float] | None = None
    nsf_fee: ExtractedField[float] | None = None
    payment_methods: ExtractedField[list[str]] | None = None


class Deposits(BaseModel):
    security_deposit: ExtractedField[float]
    pet_deposit: ExtractedField[float] | None = None
    pet_fee_nonrefundable: ExtractedField[float] | None = None
    last_month_rent: ExtractedField[float] | None = None
    key_deposit: ExtractedField[float] | None = None


class Utilities(BaseModel):
    electric: ExtractedField[UtilityResponsibility]
    gas: ExtractedField[UtilityResponsibility]
    water: ExtractedField[UtilityResponsibility]
    sewer: ExtractedField[UtilityResponsibility]
    trash: ExtractedField[UtilityResponsibility]
    internet: ExtractedField[UtilityResponsibility] | None = None
    cable: ExtractedField[UtilityResponsibility] | None = None


class Pets(BaseModel):
    pets_allowed: ExtractedField[bool]
    pet_count_limit: ExtractedField[int] | None = None
    weight_limit_lbs: ExtractedField[int] | None = None
    breed_restrictions: ExtractedField[list[str]] | None = None
    monthly_pet_rent: ExtractedField[float] | None = None


class SpecialClauses(BaseModel):
    early_termination_allowed: ExtractedField[bool]
    early_termination_fee: ExtractedField[float] | None = None
    military_clause: ExtractedField[bool] | None = None
    renewal_option: ExtractedField[bool] | None = None
    sublet_allowed: ExtractedField[bool] | None = None
    guest_stay_limit_days: ExtractedField[int] | None = None


class ComplianceDisclosures(BaseModel):
    """Federally or state-mandated disclosures. Presence + signature, not the content."""
    lead_paint_disclosure: ExtractedField[bool]  # Federal, pre-1978 properties
    mold_disclosure: ExtractedField[bool] | None = None
    bed_bug_disclosure: ExtractedField[bool] | None = None
    asbestos_disclosure: ExtractedField[bool] | None = None
    flood_zone_disclosure: ExtractedField[bool] | None = None  # Required in some states


# ---------------------------------------------------------------------------
# Top-level extraction
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2
"""Versioning for the LeaseExtraction shape so future migrations are explicit.

Version history:
  - 1: `SourceSpan.bbox: BoundingBox | None` (Sonnet-emitted single bbox).
  - 2: `SourceSpan.bboxes: list[BoundingBox]` (OCR-anchored per-line) +
       `match_type` + `section_label`. `bbox` kept for one cycle for in-flight
       DB rows. Current.

Bump on any structural change to LeaseExtraction or its nested models that
breaks deserialization of older rows. Add the migration note inline here.
"""


class LeaseExtraction(BaseModel):
    """The full structured representation of an extracted lease."""
    lease_id: UUID
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    template_detected: LeaseTemplate
    parties: list[Party]
    property: Property
    term: Term
    rent: Rent
    deposits: Deposits
    utilities: Utilities
    pets: Pets
    special_clauses: SpecialClauses
    compliance: ComplianceDisclosures
    overall_confidence: float = Field(..., ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Exception queue
# ---------------------------------------------------------------------------

class ExceptionType(StrEnum):
    MISSING_REQUIRED_FIELD = "missing_required_field"
    LOW_CONFIDENCE = "low_confidence"
    INTERNAL_INCONSISTENCY = "internal_inconsistency"
    UNUSUAL_CLAUSE = "unusual_clause"
    COMPLIANCE_GAP = "compliance_gap"


class ExceptionSeverity(StrEnum):
    BLOCKING = "blocking"      # Must be resolved before extraction is usable
    WARNING = "warning"        # Should review; not blocking
    INFORMATIONAL = "informational"


class ReviewAction(StrEnum):
    APPROVE = "approve"        # Accept the model's extraction as-is
    EDIT = "edit"              # Replace with a human correction
    REJECT = "reject"          # Mark the field as unrecoverable from this document


class LeaseException(BaseModel):
    exception_id: UUID
    lease_id: UUID
    field_path: str            # e.g. "rent.late_fee_flat" — dot-path into LeaseExtraction
    exception_type: ExceptionType
    severity: ExceptionSeverity
    description: str
    suggested_action: str | None = None
    resolved: bool = False
    resolution: ReviewAction | None = None
    correction: dict | None = None  # Free-form; field-typed value lives here on EDIT

"""
JUTAKU calculation logic — mirrors the three flows in the
"JUTAKU — DM Rent & DM Share Flows" diagram:

1. DM RENT   : ^X = X * 1.1 * 1.1
2. DM SHARE  : ^X = X * Y * 1.1 * 1.1 ; final_share = ^X / Y
3. DM LOAN   : eligible_amount = 0.44 * monthly_take_home
               benchmark = 36827
               compares eligible_amount against the benchmark
"""

from dataclasses import dataclass

LOAN_BENCHMARK = 36827.0
GST_MULTIPLIER = 1.1  # applied twice, matching the diagram (x1.1 x1.1)


@dataclass
class RentResult:
    x: float
    x_hat: float

    def message(self) -> str:
        return (
            f"📄 *DM Rent calculation*\n"
            f"Base rent (X): ₹{self.x:,.2f}\n"
            f"With charges applied (X̂ = X × 1.1 × 1.1): ₹{self.x_hat:,.2f}\n\n"
            f"👉 The amount payable is ₹{self.x_hat:,.2f}."
        )


@dataclass
class ShareResult:
    x: float
    y: float
    x_hat: float
    final_share: float

    def message(self) -> str:
        return (
            f"📄 *DM Share calculation*\n"
            f"Base amount (X): ₹{self.x:,.2f}\n"
            f"Number of sharers (Y): {self.y:g}\n"
            f"X̂ = X × Y × 1.1 × 1.1 = ₹{self.x_hat:,.2f}\n"
            f"Final share per person (X̂ ÷ Y): ₹{self.final_share:,.2f}\n\n"
            f"👉 Each person pays ₹{self.final_share:,.2f}."
        )


@dataclass
class LoanResult:
    monthly_take_home: float
    eligible_amount: float
    benchmark: float
    meets_benchmark: bool

    def message(self) -> str:
        verdict = (
            "✅ meets the ₹{:.0f} benchmark — you qualify.".format(self.benchmark)
            if self.meets_benchmark
            else "❌ is below the ₹{:.0f} benchmark — you don't currently qualify.".format(
                self.benchmark
            )
        )
        return (
            f"📄 *DM Loan eligibility*\n"
            f"Monthly take-home: ₹{self.monthly_take_home:,.2f}\n"
            f"44% of take-home: ₹{self.eligible_amount:,.2f}\n\n"
            f"👉 Your eligible amount (₹{self.eligible_amount:,.2f}) {verdict}"
        )


def calculate_rent(x: float) -> RentResult:
    x_hat = x * GST_MULTIPLIER * GST_MULTIPLIER
    return RentResult(x=x, x_hat=x_hat)


def calculate_share(x: float, y: float) -> ShareResult:
    x_hat = x * y * GST_MULTIPLIER * GST_MULTIPLIER
    final_share = x_hat / y
    return ShareResult(x=x, y=y, x_hat=x_hat, final_share=final_share)


def calculate_loan(monthly_take_home: float) -> LoanResult:
    eligible_amount = 0.44 * monthly_take_home
    meets = eligible_amount >= LOAN_BENCHMARK
    return LoanResult(
        monthly_take_home=monthly_take_home,
        eligible_amount=eligible_amount,
        benchmark=LOAN_BENCHMARK,
        meets_benchmark=meets,
    )

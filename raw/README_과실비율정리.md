# 교통사고 과실비율 기준 정리

## 산출물

- `fault_ratio_cases_normalized.csv`: 표 기반 정규화 CSV. 스프레드시트 검토와 간단한 검색용
- `fault_ratio_cases_normalized.json`: 메타데이터와 검증 결과를 포함한 전체 JSON
- `fault_ratio_cases_for_app.json`: 웹사이트/앱에서 바로 읽기 쉬운 경량 JSON
- `fault_ratio_review_required.csv`: 원문에 수치형 기본 과실비율이 없어 별도 검토가 필요한 항목
- `extraction_validation_report.txt`: 추출 건수와 검증 결과

## 데이터 사용 방식

1. 사고 설명을 입력받아 `category`, `title`, `partyA.description`, `partyB.description`, `keywords`에서 후보를 검색합니다.
2. 후보가 여러 개면 `variant` 조건을 사용자에게 선택하게 합니다.
3. 선택된 레코드의 `partyA.baseFault`, `partyB.baseFault`를 기본 과실비율로 표시합니다.
4. 추가 사실관계가 있으면 `modifiers` 항목을 체크박스로 선택하게 하고, `delta_party_a`, `delta_party_b`를 더해 조정합니다.
5. 최종 비율은 0~100 범위로 보정하고, A+B 합이 100이 되도록 반대쪽 비율을 함께 조정하는 방식이 안전합니다.

## 주의

- 이 데이터는 PDF 원문 표를 앱용으로 정리한 참고자료이며, 실제 분쟁/소송/보험 판단은 구체적 증거와 법률 검토가 필요합니다.
- 원문에서 보행자 기본 과실비율로 제시된 표는 자동차/보행자 합계가 100이 되도록 환산했습니다.
- 보35/보36은 원문상 보행자가 A, 자동차가 B이나, 다른 보행자 사고와 맞추기 위해 자동차/보행자 기준으로 환산했습니다.
- 한 사고번호에 여러 조건이 있는 경우 레코드가 여러 개로 나뉩니다.


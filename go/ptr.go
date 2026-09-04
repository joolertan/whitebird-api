package whitebird

// Ptr returns a pointer to v. Optional request fields are pointers, so that an unset field can be
// told apart from a zero value:
//
//	params := whitebird.BrokerageSubaccountsListParams{Size: 50}
//	filter := whitebird.BrokerageOTCOrdersListParams{FromAsset: whitebird.Ptr("USDT")}
func Ptr[T any](v T) *T {
	return &v
}

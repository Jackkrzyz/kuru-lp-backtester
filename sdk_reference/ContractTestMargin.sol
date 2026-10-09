// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// Test-only ledger. Trading and flip execution run in the real OrderBook.
contract ContractTestMargin {
    receive() external payable {}
    fallback() external { revert("unknown ledger selector"); }
    mapping(address => mapping(address => uint256)) public balances;
    function fund(address user, address asset, uint256 amount) external { balances[user][asset] += amount; }
    function debitUser(address user, address asset, uint256 amount) external {
        require(balances[user][asset] >= amount, "insufficient test balance");
        balances[user][asset] -= amount;
    }
    function creditUser(address user, address asset, uint256 amount, bool) external { balances[user][asset] += amount; }
    function creditUsersEncoded(bytes calldata data) external returns (uint256) {
        require(data.length % 128 == 0, "unexpected credit encoding length");
        for (uint256 offset; offset < data.length; offset += 128) {
            (address user, address asset, uint256 amount,) = abi.decode(data[offset:offset+128], (address,address,uint256,bool));
            balances[user][asset] += amount;
        }
        return 0;
    }
    function creditFee(address, uint256, address, uint256) external {}
    function cycle(address book, uint96 sellSize, uint96 buyQuote) external {
        (bool sold,) = book.call(abi.encodeWithSignature("placeAndExecuteMarketSell(uint96,uint256,bool,bool)", sellSize,0,true,false));
        require(sold, "sell failed");
        (bool bought,) = book.call(abi.encodeWithSignature("placeAndExecuteMarketBuy(uint96,uint256,bool,bool)", buyQuote,0,true,false));
        require(bought, "buy failed");
    }
}

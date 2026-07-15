// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";

contract DexPair {

    address public token0;
    address public token1;

    uint256 public reserve0;
    uint256 public reserve1;

    constructor(
        address _token0,
        address _token1
    ) {
        token0 = _token0;
        token1 = _token1;
    }

    function addLiquidity(
        uint256 amount0,
        uint256 amount1
    ) external {

        IERC20(token0).transferFrom(
            msg.sender,
            address(this),
            amount0
        );

        IERC20(token1).transferFrom(
            msg.sender,
            address(this),
            amount1
        );

        reserve0 += amount0;
        reserve1 += amount1;
    }

    function getReserves()
        external
        view
        returns (
            uint256,
            uint256
        )
    {
        return (
            reserve0,
            reserve1
        );
    }

    function swapToken0ForToken1(
        uint256 amountIn
    ) external {

        require(
            reserve0 > 0 &&
            reserve1 > 0,
            "NO_LIQUIDITY"
        );

        IERC20(token0).transferFrom(
            msg.sender,
            address(this),
            amountIn
        );

        uint256 amountOut =
            (amountIn * reserve1) /
            (reserve0 + amountIn);

        require(
            amountOut < reserve1,
            "INSUFFICIENT_LIQUIDITY"
        );

        IERC20(token1).transfer(
            msg.sender,
            amountOut
        );

        reserve0 += amountIn;
        reserve1 -= amountOut;
    }
}

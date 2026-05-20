#!/usr/bin/env python3
"""生成可视化图表"""
import json
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')  # 使用非交互式后端

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial']
plt.rcParams['axes.unicode_minus'] = False

def load_data():
    """加载分析数据"""
    with open('expert_analysis.json', 'r', encoding='utf-8') as f:
        return json.load(f)

def plot_category_comparison(data):
    """各类别平均分对比"""
    fig, ax = plt.subplots(figsize=(10, 6))

    categories = []
    avg_scores = []
    std_devs = []

    for cat, stats in sorted(data['by_category'].items(),
                            key=lambda x: x[1]['avg_score'],
                            reverse=True):
        categories.append(cat.replace('_', '\n'))
        avg_scores.append(stats['avg_score'])
        std_devs.append(stats['std_dev'])

    bars = ax.bar(categories, avg_scores, yerr=std_devs,
                  capsize=5, alpha=0.7, color='steelblue')

    # 添加数值标签
    for bar, score in zip(bars, avg_scores):
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height,
                f'{score:.1f}',
                ha='center', va='bottom', fontsize=10, fontweight='bold')

    ax.set_ylabel('Average Score', fontsize=12)
    ax.set_title('Expert Performance by Category', fontsize=14, fontweight='bold')
    ax.grid(axis='y', alpha=0.3)

    plt.tight_layout()
    plt.savefig('category_comparison.png', dpi=150, bbox_inches='tight')
    print("✓ 生成图表: category_comparison.png")
    plt.close()

def plot_time_trend(data):
    """时间趋势图"""
    fig, ax = plt.subplots(figsize=(10, 6))

    periods = ['EARLY', 'MIDDLE', 'LATE']
    scores = []
    counts = []

    for period in ['early', 'middle', 'late']:
        if period in data['time_trend']:
            scores.append(data['time_trend'][period]['avg_score'])
            counts.append(data['time_trend'][period]['count'])

    # 绘制折线图
    line = ax.plot(periods, scores, marker='o', linewidth=2.5,
                   markersize=10, color='#2E86AB', label='Avg Score')

    # 添加数值标签
    for i, (period, score, count) in enumerate(zip(periods, scores, counts)):
        ax.text(i, score + 0.5, f'{score:.1f}\n({count} tasks)',
                ha='center', va='bottom', fontsize=10, fontweight='bold')

    # 添加趋势线
    ax.axhline(y=scores[0], color='gray', linestyle='--', alpha=0.5, label='Baseline')

    # 计算提升百分比
    improvement = ((scores[-1] - scores[0]) / scores[0]) * 100
    ax.text(1, max(scores) * 0.95,
            f'Overall Improvement: +{improvement:.1f}%',
            ha='center', fontsize=12, fontweight='bold',
            bbox=dict(boxstyle='round', facecolor='lightgreen', alpha=0.7))

    ax.set_ylabel('Average Score', fontsize=12)
    ax.set_title('Performance Trend Over Time (Refine Effect)',
                 fontsize=14, fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=10)

    plt.tight_layout()
    plt.savefig('time_trend.png', dpi=150, bbox_inches='tight')
    print("✓ 生成图表: time_trend.png")
    plt.close()

def plot_score_distribution(data):
    """各类别得分分布箱线图"""
    fig, ax = plt.subplots(figsize=(12, 6))

    categories = []
    scores_list = []

    for cat, stats in sorted(data['by_category'].items()):
        categories.append(cat.replace('_', '\n'))
        scores_list.append(stats['scores'])

    bp = ax.boxplot(scores_list, labels=categories, patch_artist=True,
                    showmeans=True, meanline=True)

    # 美化箱线图
    for patch in bp['boxes']:
        patch.set_facecolor('lightblue')
        patch.set_alpha(0.7)

    for median in bp['medians']:
        median.set_color('red')
        median.set_linewidth(2)

    for mean in bp['means']:
        mean.set_color('green')
        mean.set_linewidth(2)

    ax.set_ylabel('Score', fontsize=12)
    ax.set_title('Score Distribution by Category', fontsize=14, fontweight='bold')
    ax.grid(axis='y', alpha=0.3)

    # 添加图例
    ax.plot([], [], 'r-', linewidth=2, label='Median')
    ax.plot([], [], 'g-', linewidth=2, label='Mean')
    ax.legend(fontsize=10)

    plt.tight_layout()
    plt.savefig('score_distribution.png', dpi=150, bbox_inches='tight')
    print("✓ 生成图表: score_distribution.png")
    plt.close()

def main():
    print("加载数据...")
    data = load_data()

    print("\n生成可视化图表...")
    plot_category_comparison(data)
    plot_time_trend(data)
    plot_score_distribution(data)

    print("\n✅ 所有图表生成完成！")
    print("图表位置: /inspire/hdd/project/26summer-camp-05/26210058/czzzl/analysis/")

if __name__ == '__main__':
    main()
